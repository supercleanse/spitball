"""Meeting reminders: a desktop notification shortly before a calendar
event with a video link starts, offering to join it and record.

The daemon runs one `Scheduler`. Off the recording path and on its own
thread, it keeps a small list of the next few hours of events (from the
same cached feed `calendar.py` uses, re-downloaded only when the cache is
older than `calendar_cache_ttl_s`); on every daemon tick it checks, from
that list and without any I/O, whether a reminder is due, and fires it on a
thread of its own so the `notify-send --wait` round trip never blocks
detection or recording. A reminder fires once per event occurrence: the
occurrence keys it has fired for are kept in `<state>/calendar/reminded.json`
so a daemon restart cannot fire again, and anything that started more than
`LATE_GRACE_S` ago (a laptop coming back from suspend, a late start) is
never reminded at all.

The link is the dangerous part: an invite can come from anyone. Only an
`https` URL whose host is on the allowlist below is ever opened, and it is
handed to `xdg-open` as one argv element -- never through a shell. The
feed address itself never comes anywhere near this module.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import calendar, config

# ---------------------------------------------------------------- tunables

# How far ahead the scheduler keeps events in memory, and how often it
# refreshes that list from the (cached) source. The network fetch itself
# still follows calendar_cache_ttl_s.
LOOKAHEAD_S = 6 * 3600
REFRESH_S = 300
# A meeting that started more than this long ago is never reminded.
LATE_GRACE_S = 120
# Keep fired occurrence keys this long, then prune them.
LOG_KEEP_S = 3 * 24 * 3600
# The longest a notification may wait for an answer, beyond the lead time.
WAIT_EXTRA_S = 600
NOTIFY_TIMEOUT_S = 5

# Hosts whose links a reminder may open. Exact names, plus any subdomain of
# the suffix entries (us04web.zoom.us, company.webex.com). Nothing else,
# whatever the invite says.
JOIN_HOSTS_EXACT = frozenset({"meet.google.com", "teams.microsoft.com", "teams.live.com"})
JOIN_HOSTS_SUFFIX = ("zoom.us", "webex.com")
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")
MAX_URL_LEN = 2048


# ---------------------------------------------------------------- links

def safe_join_url(url) -> str:
    """`url` when it is an https link to an allowlisted host; "" otherwise.
    Strict on purpose: no other scheme (http, javascript, zoommtg, data),
    no userinfo (`https://zoom.us@evil.example`), no lookalike host
    (`meet.google.com.evil.example`, `evilzoom.us`), no non-ASCII host, no
    control or whitespace characters anywhere, no odd port."""
    if not isinstance(url, str):
        return ""
    u = url.strip()
    if not u or len(u) > MAX_URL_LEN:
        return ""
    if any(ord(c) < 33 or ord(c) > 126 for c in u) or "\\" in u:
        return ""
    try:
        parts = urllib.parse.urlsplit(u)
    except ValueError:
        return ""
    if parts.scheme.lower() != "https":
        return ""
    if "@" in parts.netloc:
        return ""
    try:
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return ""
    host = host.lower()  # a trailing dot (meet.google.com.) fails the host regex on purpose
    if not host or not _HOST_RE.match(host) or port not in (None, 443):
        return ""
    if host in JOIN_HOSTS_EXACT or any(host == s or host.endswith("." + s) for s in JOIN_HOSTS_SUFFIX):
        return u
    return ""


def join_link(ev: dict) -> str:
    """The event's meeting link if it is safe to open, else ""."""
    if not isinstance(ev, dict):
        return ""
    conf = ev.get("conference")
    link = safe_join_url(conf.get("url")) if isinstance(conf, dict) else ""
    if link and "?" in link:
        return link
    # Either no usable conference link, or one without a query string (a
    # `calendar_command` source may hand over the bare id link while the
    # location carries the invitation link with its passcode): look through
    # the text fields for an allowlisted https link, preferring one that is
    # the same link with its query string attached.
    found = ""
    for field in ("location", "url", "description"):
        text = ev.get(field)
        if not isinstance(text, str) or not text:
            continue
        for m in calendar._URL_RE.finditer(text):
            cand = safe_join_url(m.group(0).rstrip(".,;:)>'\""))
            if not cand:
                continue
            if link and cand.startswith(link + "?"):
                return cand
            found = found or cand
    return link or found


def link_host(link: str) -> str:
    try:
        return (urllib.parse.urlsplit(link).hostname or "").lower()
    except ValueError:
        return ""


def open_link(link: str) -> bool:
    """Opens an allowlisted link with xdg-open, one argv element, detached.
    Refuses anything safe_join_url would not pass."""
    if not safe_join_url(link):
        return False
    try:
        subprocess.Popen(["xdg-open", link], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, start_new_session=True)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- eligibility and timing

def skip_reason(ev: dict) -> str:
    """Why an event gets no reminder -- "" when it does."""
    why = calendar.hard_filter(ev)
    if why:
        return why
    if not join_link(ev):
        return "no meeting link"
    return ""


def occurrence_key(ev: dict) -> str:
    """One occurrence of one event at one time: a rescheduled meeting is
    reminded again, the same one is not."""
    return f"{ev.get('id', '')}@{ev.get('start', '')}"


def lead_seconds(cfg: dict) -> int:
    try:
        return max(0, min(3600, int(cfg.get("calendar_remind_before_s", 60))))
    except (TypeError, ValueError):
        return 60


def enabled(cfg: dict) -> bool:
    return bool(cfg.get("calendar_enabled")) and bool(cfg.get("calendar_reminders", True)) \
        and calendar.source_kind(cfg) != "off"


def plan(events: list, now: float, lead_s: int, reminded=None, late_grace_s: float = LATE_GRACE_S) -> tuple:
    """(upcoming, skipped): `upcoming` is every eligible event whose start is
    not more than `late_grace_s` in the past, oldest first, each as
    {"event", "key", "link", "host", "start", "fire_at", "reminded"};
    `skipped` counts the events in the list that get no reminder."""
    reminded = reminded or set()
    out, skipped = [], 0
    for ev in events:
        if skip_reason(ev):
            skipped += 1
            continue
        try:
            start = calendar.from_iso(ev["start"]).timestamp()
        except (KeyError, ValueError, TypeError):
            skipped += 1
            continue
        if start + late_grace_s < now:
            continue
        link = join_link(ev)
        key = occurrence_key(ev)
        out.append({"event": ev, "key": key, "link": link, "host": link_host(link), "start": start,
                    "fire_at": start - lead_s, "reminded": key in reminded})
    out.sort(key=lambda r: (r["start"], r["event"].get("title", "")))
    return out, skipped


def due(events: list, now: float, lead_s: int, reminded, late_grace_s: float = LATE_GRACE_S) -> list:
    """The reminders to fire right now: due, not yet fired, not stale."""
    upcoming, _ = plan(events, now, lead_s, reminded, late_grace_s)
    return [r for r in upcoming if not r["reminded"] and r["fire_at"] <= now]


def load_window(cfg: dict, now: float, lookahead_s: float = LOOKAHEAD_S) -> tuple:
    """(events, info) from the configured source for [now - grace, now +
    lookahead]. The feed is served from the cache while fresh."""
    t = datetime.fromtimestamp(now, tz=timezone.utc)
    return calendar.load_events(cfg, t - timedelta(seconds=LATE_GRACE_S + 3600),
                                t + timedelta(seconds=lookahead_s))


# ---------------------------------------------------------------- fired-reminder log

class ReminderLog:
    """The occurrence keys already reminded, on disk so a restart doesn't
    fire them again. Keys are UID-plus-time, never titles."""

    def __init__(self, path: Path | None = None):
        self.path = path or (config.STATE_DIR / "calendar" / "reminded.json")
        self._fired: dict = {}
        self._loaded = False

    def _load(self):
        if self._loaded:
            return
        self._loaded = True
        try:
            data = json.loads(self.path.read_text())
            if isinstance(data, dict):
                self._fired = {str(k): float(v) for k, v in data.items()
                               if isinstance(v, (int, float))}
        except (OSError, ValueError, TypeError):
            self._fired = {}

    def keys(self) -> set:
        self._load()
        return set(self._fired)

    def __contains__(self, key: str) -> bool:
        self._load()
        return key in self._fired

    def mark(self, key: str, now: float) -> None:
        self._load()
        self._fired[key] = now
        self.prune(now)
        self._save()

    def prune(self, now: float, keep_s: float = LOG_KEEP_S) -> None:
        self._load()
        self._fired = {k: v for k, v in self._fired.items() if now - v < keep_s}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            config.atomic_write(self.path, json.dumps(self._fired), mode=0o600)
        except OSError:
            pass


# ---------------------------------------------------------------- the notification

def _clock(ts: float) -> str:
    d = datetime.fromtimestamp(ts, tz=calendar.local_tz())
    return f"{d:%-I:%M %p}"


def when_label(start: float, now: float) -> str:
    delta = start - now
    if delta > 45:
        return f"in {max(1, math.ceil(delta / 60))} min"
    if delta >= -45:
        return "now"
    return f"started {max(1, round(-delta / 60))} min ago"


def notification_text(entry: dict, now: float, actions: bool) -> tuple:
    """(summary, body). The summary names Spitball so the toast can never be
    mistaken for the meeting app's own; the body carries the time, the link's
    host, and what a click does."""
    ev = entry["event"]
    summary = f"Spitball reminder: {ev.get('title') or '(untitled)'} {when_label(entry['start'], now)}"
    try:
        end = calendar.from_iso(ev["end"]).timestamp()
        span = f"{_clock(entry['start'])}–{_clock(end)}"
    except (KeyError, ValueError, TypeError):
        span = _clock(entry["start"])
    what = ("Click to join and record." if actions
            else "Join from your calendar, then click the record button in the bar.")
    return summary, f"{span} · {entry['host']}\n{what}"


def server_capabilities(run=subprocess.run) -> list | None:
    """The notification server's capabilities, via a read-only D-Bus call;
    None when they can't be read (no gdbus, no server, a timeout)."""
    try:
        r = run(["gdbus", "call", "--session", "--dest", "org.freedesktop.Notifications",
                 "--object-path", "/org/freedesktop/Notifications",
                 "--method", "org.freedesktop.Notifications.GetCapabilities"],
                capture_output=True, text=True, timeout=NOTIFY_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return re.findall(r"'([^']*)'", r.stdout or "")


def desktop_notify(summary: str, body: str, actions: list, wait_s: float, expire_ms: int,
                   run=subprocess.run) -> dict:
    """One `notify-send` round trip. With `actions` ([(name, label), ...])
    it waits for the user's answer and returns it in `action` ("" when the
    toast closed or expired without one). {"action", "rc", "stderr", "timed_out"}."""
    cmd = ["notify-send", "--app-name", "Spitball", "-u", "normal", "-i", "x-office-calendar",
           "-t", str(int(expire_ms))]
    for name, label in actions:
        cmd += ["-A", f"{name}={label}"]
    cmd += [summary, body]
    try:
        r = run(cmd, capture_output=True, text=True, timeout=wait_s if actions else NOTIFY_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"action": "", "rc": -1, "stderr": "", "timed_out": True}
    except (OSError, subprocess.SubprocessError) as e:
        return {"action": "", "rc": -1, "stderr": e.__class__.__name__, "timed_out": False}
    action = (r.stdout or "").strip().splitlines()
    return {"action": action[0].strip() if action else "", "rc": r.returncode,
            "stderr": r.stderr or "", "timed_out": False}


# ---------------------------------------------------------------- the scheduler

class Scheduler:
    """Owns the reminder loop for one daemon. `cfg` is a callable returning
    the current config (so `reload` applies); `recording` says whether a
    recording is running; `join(event, link)` starts one pinned to the
    event. `notify`/`opener`/`loader`/`clock`/`capabilities` are injection
    points for tests; the defaults are the real desktop."""

    JOIN = "default"      # the action a plain click fires on every server
    DISMISS = "dismiss"

    def __init__(self, cfg, recording, join, notify=desktop_notify, opener=open_link,
                 loader=load_window, clock=time.time, capabilities=server_capabilities,
                 log: ReminderLog | None = None):
        self._cfg = cfg
        self._recording = recording
        self._join = join
        self._notify = notify
        self._opener = opener
        self._loader = loader
        self._clock = clock
        self._capabilities = capabilities
        self.log = log or ReminderLog()
        self.lock = threading.Lock()
        self.events: list = []
        self.loaded_at = 0.0
        self.error = ""
        self.actions_ok: bool | None = None   # None = not checked yet
        self.caps_checked_at = 0.0
        self.pending: set = set()             # keys with a toast on screen
        self.fired = 0
        self._refreshing = False

    # ----- the per-second tick, called from the daemon loop; cheap, no I/O

    def tick(self) -> None:
        try:
            cfg = self._cfg()
            if not enabled(cfg):
                return
            now = self._clock()
            if now - self.loaded_at >= REFRESH_S:
                self._start_refresh(cfg, now)
            with self.lock:
                events = list(self.events)
            reminded = self.log.keys() | self.pending
            for entry in due(events, now, lead_seconds(cfg), reminded):
                if self._recording():
                    self.log.mark(entry["key"], now)   # mid-call: never interrupt
                    continue
                self.pending.add(entry["key"])
                self.log.mark(entry["key"], now)
                threading.Thread(target=self._fire, args=(entry, now, cfg), daemon=True,
                                 name="reminder").start()
        except Exception as e:  # pragma: no cover - nothing here may reach the daemon loop
            self.error = f"{e.__class__.__name__}: {e}"

    def _start_refresh(self, cfg: dict, now: float) -> None:
        with self.lock:
            if self._refreshing:
                return
            self._refreshing = True
        self.loaded_at = now
        threading.Thread(target=self._refresh, args=(cfg, now), daemon=True, name="reminder-feed").start()

    def _refresh(self, cfg: dict, now: float) -> None:
        try:
            events, info = self._loader(cfg, now)
            with self.lock:
                self.events = list(events)
                self.error = str(info.get("error") or "") if isinstance(info, dict) else ""
        except Exception as e:
            with self.lock:
                self.error = f"reminder lookup failed ({e.__class__.__name__}: {e})"
        finally:
            with self.lock:
                self._refreshing = False

    def refresh_now(self) -> None:
        """Synchronous refresh (tests, and the first tick after start)."""
        cfg = self._cfg()
        self.loaded_at = self._clock()
        self._refresh(cfg, self.loaded_at)

    # ----- one notification, on its own thread

    def _actions_supported(self, now: float) -> bool:
        if self.actions_ok is None or now - self.caps_checked_at > 3600:
            caps = self._capabilities()
            self.caps_checked_at = now
            # Unknown (no gdbus, no answer): try actions anyway; notify-send
            # itself degrades to a plain toast and says so on stderr.
            self.actions_ok = True if caps is None else ("actions" in caps)
        return bool(self.actions_ok)

    def _fire(self, entry: dict, now: float, cfg: dict) -> None:
        try:
            actions_ok = self._actions_supported(now)
            summary, body = notification_text(entry, now, actions_ok)
            lead = lead_seconds(cfg)
            actions = [(self.JOIN, "Join & record"), (self.DISMISS, "Dismiss")] if actions_ok else []
            result = self._notify(summary, body, actions, wait_s=lead + WAIT_EXTRA_S,
                                  expire_ms=min(lead + 120, 600) * 1000)
            self.fired += 1
            if actions_ok and "not supported" in (result.get("stderr") or "").lower():
                self.actions_ok = False   # the server lied, or changed; next time plain
            if result.get("action") == self.JOIN:
                self._do_join(entry)
        except Exception as e:  # pragma: no cover
            self.error = f"reminder failed ({e.__class__.__name__}: {e})"
        finally:
            self.pending.discard(entry["key"])

    def _do_join(self, entry: dict) -> None:
        link = safe_join_url(entry["link"])
        if not link:
            return
        self._opener(link)
        if not self._recording():
            self._join(entry["event"], link)

    # ----- for `calendar upcoming` and status

    def snapshot(self, now: float | None = None) -> dict:
        now = self._clock() if now is None else now
        cfg = self._cfg()
        with self.lock:
            events = list(self.events)
        upcoming, skipped = plan(events, now, lead_seconds(cfg), self.log.keys())
        return {"enabled": enabled(cfg), "lead_s": lead_seconds(cfg), "loaded_at": self.loaded_at,
                "error": self.error, "upcoming": upcoming, "skipped": skipped, "fired": self.fired}


# ---------------------------------------------------------------- CLI

def upcoming_report(cfg: dict, now: float | None = None, hours: float = 24, refresh: bool = False) -> dict:
    """`spitball calendar upcoming --json`: the reminders the daemon would
    fire from now, from the same cached source. Works with reminders off (it
    says so) so the list can be checked before switching them on."""
    now = time.time() if now is None else now
    kind = calendar.source_kind(cfg)
    rep = {"ok": False, "enabled": bool(cfg.get("calendar_enabled")), "reminders": bool(cfg.get("calendar_reminders", True)),
           "active": enabled(cfg), "lead_s": lead_seconds(cfg), "source": kind, "error": "", "at": now,
           "hours": hours, "upcoming": [], "skipped": 0, "fetched_at": 0, "cached": False}
    if kind == "off":
        rep["error"] = "no calendar source configured"
        return rep
    t = datetime.fromtimestamp(now, tz=timezone.utc)
    try:
        if refresh and kind == "ics":
            calendar.fetch_ics(cfg, refresh=True, now=now)
        events, info = calendar.load_events(cfg, t - timedelta(seconds=LATE_GRACE_S + 3600),
                                            t + timedelta(hours=hours))
    except Exception as e:  # pragma: no cover
        rep["error"] = calendar._redact(f"lookup failed ({e.__class__.__name__}: {e})", calendar._feed_url(cfg))
        return rep
    rep.update(error=str(info.get("error") or ""), fetched_at=info.get("fetched_at", 0),
               cached=bool(info.get("cached")))
    rep["ok"] = not rep["error"] or rep["cached"]
    reminded = ReminderLog().keys()
    upcoming, skipped = plan(events, now, lead_seconds(cfg), reminded)
    rep["skipped"] = skipped
    rep["upcoming"] = [{"id": r["event"].get("id", ""), "title": r["event"].get("title", ""),
                        "start": r["event"].get("start", ""), "end": r["event"].get("end", ""),
                        "host": r["host"], "link": r["link"], "fire_at": r["fire_at"],
                        "reminded": r["reminded"],
                        "attendees": len([a for a in r["event"].get("attendees") or []
                                          if not a.get("self") and a.get("response") != "declined"])}
                       for r in upcoming]
    return rep


def format_upcoming_report(rep: dict) -> str:
    now = rep.get("at", time.time())
    state = "on" if rep.get("active") else ("off" if not rep.get("enabled") else
                                             "off (calendar_reminders is false)")
    lines = [f"Reminders: {state}; {rep.get('lead_s', 60)} s before; source: {rep.get('source')}"]
    if rep.get("error"):
        lines.append(("Using a cached feed: " if rep.get("cached") else "Source: FAILED -- ") + rep["error"])
    ups = rep.get("upcoming") or []
    if not ups:
        lines.append(f"No meetings with a video link in the next {int(rep.get('hours', 24))} hours.")
    else:
        lines.append(f"Next {len(ups)} in {int(rep.get('hours', 24))} hours:")
        for r in ups:
            try:
                start = calendar.from_iso(r["start"]).timestamp()
                end = calendar.from_iso(r["end"]).timestamp()
                day = datetime.fromtimestamp(start, tz=calendar.local_tz())
                when = f"{day:%a %b %-d} {_clock(start)}–{_clock(end)}"
            except (KeyError, ValueError, TypeError):
                when, start = r.get("start", ""), now
            tag = "fired" if r.get("reminded") else f"reminder at {_clock(r.get('fire_at', start))}"
            if not r.get("reminded") and r.get("fire_at", start) <= now:
                tag = "due now"
            lines.append(f"  - {when}  {r.get('title', '')}  ({r.get('host', '')}) -- {tag}")
    if rep.get("skipped"):
        n = rep["skipped"]
        lines.append(f"Skipped: {n} event{'s' if n != 1 else ''} in the window with no video link, "
                     "all-day, declined, marked free, or canceled")
    return "\n".join(lines)
