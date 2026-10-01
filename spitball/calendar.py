"""Calendar: match a recording to the meeting it belongs to (docs/SPEC-v2.md §2).

Two sources, both standard-library only:

- `calendar_ics_url` (a secret, e.g. Google Calendar's "Secret address in iCal
  format", or any ICS/webcal URL) -- fetched over HTTPS, cached on disk under
  ~/.local/state/spitball/calendar/ for `calendar_cache_ttl_s`, parsed here
  (RFC 5545 line unfolding, quoted parameters, TZID via zoneinfo, a bounded
  RRULE/EXDATE/RECURRENCE-ID expander around the window that matters).
- `calendar_command` -- any shell command whose stdout is a JSON array of
  events in the normalized shape below (CONTRACT.md "Calendar events").

Both produce the same normalized event dicts, and one deterministic matcher
(`match`) picks the event for a recording: a time-overlap window, hard
filters (all-day, canceled, free/transparent, focus/out-of-office, declined),
then a score in which a Google Meet code seen in a window title is the one
exact key. Below a threshold, or without a clear margin over the runner-up,
there is no match -- a wrong title on a call folder is worse than none.

Nothing here ever raises into the daemon or the processing pipeline: every
entry point used by them (`snapshot`, `decide`, `for_call`) catches its own
failures and reports them in the returned dict. Calendar trouble never blocks
or delays a recording. The feed URL is a bearer credential: it is never
logged, printed, or written anywhere but config.json (via `set-secret`).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import config

# ---------------------------------------------------------------- tunables

# A recording may begin this long BEFORE an event starts (early joiners)...
WINDOW_BEFORE_S = 15 * 60
# ...or this long AFTER an event has ended (running over) and still count.
WINDOW_AFTER_S = 10 * 60
# Events are expanded this far either side of the recording start, so a long
# event that began hours earlier is still a candidate.
LOOKUP_PAD_S = 24 * 3600
# The best candidate must score at least this, and beat the runner-up by the
# margin, to be used for naming/headers.
MATCH_THRESHOLD = 40
MATCH_MARGIN = 15
FETCH_TIMEOUT_S = 15
COMMAND_TIMEOUT_S = 20
HYPRCTL_TIMEOUT_S = 3
# Per-RRULE hard cap on loop iterations, so a malformed rule can't spin.
MAX_RULE_ITERATIONS = 20000
# Longest description kept in a snapshot (Google's Meet boilerplate alone
# runs to ~1 KB).
MAX_DESCRIPTION_CHARS = 4000

MEET_CODE_RE = re.compile(r"\b([a-z]{3}-[a-z]{4}-[a-z]{3})\b")
_MEET_URL_RE = re.compile(r"https?://meet\.google\.com/([a-z]{3}-[a-z]{4}-[a-z]{3})\b", re.I)
_ZOOM_URL_RE = re.compile(r"https?://[\w.-]*zoom\.us/(?:j|my|s)/([\w.-]+)", re.I)
_TEAMS_URL_RE = re.compile(r"https?://teams\.(?:microsoft|live)\.com/[^\s\"<>]+", re.I)
_WEBEX_URL_RE = re.compile(r"https?://[\w.-]*webex\.com/[^\s\"<>]+", re.I)
_URL_RE = re.compile(r"https?://[^\s\"<>]+", re.I)

_WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}

# Outlook-published feeds name zones the Windows way. A short map of the
# common ones; anything else falls back to the machine's own zone.
_WINDOWS_TZ = {
    "Eastern Standard Time": "America/New_York",
    "Central Standard Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver",
    "US Mountain Standard Time": "America/Phoenix",
    "Pacific Standard Time": "America/Los_Angeles",
    "Alaskan Standard Time": "America/Anchorage",
    "Hawaiian Standard Time": "Pacific/Honolulu",
    "GMT Standard Time": "Europe/London",
    "W. Europe Standard Time": "Europe/Berlin",
    "Romance Standard Time": "Europe/Paris",
    "Central Europe Standard Time": "Europe/Budapest",
    "Central European Standard Time": "Europe/Warsaw",
    "E. Europe Standard Time": "Europe/Chisinau",
    "FLE Standard Time": "Europe/Kiev",
    "India Standard Time": "Asia/Kolkata",
    "Singapore Standard Time": "Asia/Singapore",
    "China Standard Time": "Asia/Shanghai",
    "Tokyo Standard Time": "Asia/Tokyo",
    "AUS Eastern Standard Time": "Australia/Sydney",
    "New Zealand Standard Time": "Pacific/Auckland",
    "UTC": "UTC",
}

# How an event's conference host lines up with the app that held the mic.
# Browsers can run any of them; a native client runs its own.
_BROWSERS = ("chrome", "chromium", "brave", "firefox")
_NATIVE = {"zoom": "zoom", "teams": "teams", "webex": "webex"}
_NO_CONFERENCE_APPS = ("slack", "discord", "signal", "whatsapp")


# ---------------------------------------------------------------- time helpers

_LOCAL_TZ = None


def local_tz():
    """The machine's zone as a DST-aware ZoneInfo when it can be named ($TZ,
    or the /etc/localtime link), else whatever datetime reports."""
    global _LOCAL_TZ
    if _LOCAL_TZ is None:
        name = os.environ.get("TZ", "").lstrip(":")
        if not name:
            try:
                target = os.readlink("/etc/localtime")
                if "zoneinfo/" in target:
                    name = target.split("zoneinfo/", 1)[1]
            except OSError:
                pass
        tz = None
        if name:
            try:
                tz = ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError):
                tz = None
        _LOCAL_TZ = tz or datetime.now().astimezone().tzinfo
    return _LOCAL_TZ


def _tz_for(tzid: str):
    if not tzid:
        return local_tz()
    try:
        return ZoneInfo(tzid)
    except (ZoneInfoNotFoundError, ValueError):
        pass
    mapped = _WINDOWS_TZ.get(tzid)
    if mapped:
        try:
            return ZoneInfo(mapped)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return local_tz()


def _parse_dt(value: str, params: dict):
    """An ICS DATE or DATE-TIME -> `date` (all-day) or an aware `datetime`.
    Floating times (no TZID, no Z) are taken as the machine's own zone."""
    v = value.strip()
    if params.get("VALUE", [""])[0].upper() == "DATE" or (len(v) == 8 and v.isdigit()):
        return date(int(v[0:4]), int(v[4:6]), int(v[6:8]))
    m = re.match(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})?(Z?)$", v)
    if not m:
        raise ValueError(f"bad date-time {v!r}")
    y, mo, d, hh, mm = (int(x) for x in m.groups()[:5])
    ss = int(m.group(6) or 0)
    if m.group(7) == "Z":
        return datetime(y, mo, d, hh, mm, ss, tzinfo=timezone.utc)
    tzid = params.get("TZID", [""])[0]
    return datetime(y, mo, d, hh, mm, ss, tzinfo=_tz_for(tzid))


def _parse_duration(value: str) -> timedelta:
    m = re.match(r"^([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$", value.strip())
    if not m:
        raise ValueError(f"bad duration {value!r}")
    sign = -1 if m.group(1) == "-" else 1
    w, d, h, mi, s = (int(x or 0) for x in m.groups()[1:])
    return sign * timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)


def to_iso(v) -> str:
    if isinstance(v, datetime):
        return v.isoformat(timespec="seconds")
    return v.isoformat()


def from_iso(s) -> datetime:
    """The inverse of to_iso, always an aware datetime: a bare date reads as
    local midnight, a naive time as local, an epoch number as UTC."""
    if isinstance(s, (int, float)):
        return datetime.fromtimestamp(float(s), tz=timezone.utc)
    text = str(s).strip()
    if re.match(r"^\d+(\.\d+)?$", text):
        return datetime.fromtimestamp(float(text), tz=timezone.utc)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if not isinstance(dt, datetime):  # a date
        dt = datetime(dt.year, dt.month, dt.day)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=local_tz())
    return dt


def _key(v) -> str:
    """A comparison key for EXDATE / RECURRENCE-ID matching: the instant for
    date-times (so TZID and Z forms agree), the day for dates."""
    if isinstance(v, datetime):
        return str(int(v.timestamp()))
    return v.isoformat()


def _as_datetime(v) -> datetime:
    if isinstance(v, datetime):
        return v
    return datetime(v.year, v.month, v.day, tzinfo=local_tz())


# ---------------------------------------------------------------- ICS parsing

def unfold(text: str) -> list:
    """RFC 5545 §3.1: a line starting with a space or tab continues the one
    before it. Accepts CRLF or LF."""
    out = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def parse_property(line: str) -> tuple:
    """`NAME;P1=a;P2="x:y";P3=b,c:value` -> (NAME, {P1: [a], ...}, value).
    Quote-aware: a parameter value in double quotes may hold `;`, `:`, `,`."""
    i, n = 0, len(line)
    in_quotes = False
    while i < n:
        c = line[i]
        if c == '"':
            in_quotes = not in_quotes
        elif c == ":" and not in_quotes:
            break
        i += 1
    head, value = line[:i], line[i + 1:]
    # Split head on ';' outside quotes.
    parts, buf, in_quotes = [], [], False
    for c in head:
        if c == '"':
            in_quotes = not in_quotes
        if c == ";" and not in_quotes:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(c)
    parts.append("".join(buf))
    name = parts[0].strip().upper()
    params: dict = {}
    for p in parts[1:]:
        k, _, v = p.partition("=")
        k = k.strip().upper()
        vals, buf, in_quotes = [], [], False
        for c in v:
            if c == '"':
                in_quotes = not in_quotes
                continue
            if c == "," and not in_quotes:
                vals.append("".join(buf))
                buf = []
            else:
                buf.append(c)
        vals.append("".join(buf))
        params.setdefault(k, []).extend(vals)
    return name, params, value


def unescape(value: str) -> str:
    out, i, n = [], 0, len(value)
    while i < n:
        c = value[i]
        if c == "\\" and i + 1 < n:
            nxt = value[i + 1]
            if nxt in ("n", "N"):
                out.append("\n")
            elif nxt in (",", ";", "\\"):
                out.append(nxt)
            else:
                out.append(nxt)
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def parse_ics(text: str) -> list:
    """Every VEVENT in the file as {"props": {NAME: [(params, raw_value), ...]}}.
    Nested components (VALARM) are skipped; VTIMEZONE blocks are ignored
    because TZIDs resolve through zoneinfo."""
    events = []
    stack: list = []
    cur = None
    for line in unfold(text):
        if not line:
            continue
        upper = line.upper()
        if upper.startswith("BEGIN:"):
            kind = upper[6:].strip()
            stack.append(kind)
            if kind == "VEVENT":
                cur = {"props": {}}
            continue
        if upper.startswith("END:"):
            kind = upper[4:].strip()
            if stack and stack[-1] == kind:
                stack.pop()
            if kind == "VEVENT" and cur is not None:
                events.append(cur)
                cur = None
            continue
        if cur is None or not stack or stack[-1] != "VEVENT":
            continue
        try:
            name, params, value = parse_property(line)
        except ValueError:
            continue
        cur["props"].setdefault(name, []).append((params, value))
    return events


def _first(props: dict, name: str):
    vals = props.get(name)
    return vals[0] if vals else None


def _text(props: dict, name: str, default: str = "") -> str:
    v = _first(props, name)
    return unescape(v[1]).strip() if v else default


# ---------------------------------------------------------------- normalization

def _mailto(value: str) -> str:
    v = value.strip()
    if v.lower().startswith("mailto:"):
        v = v[7:]
    return v.strip().lower()


def _attendees(props: dict, self_emails: set) -> list:
    out = []
    for params, value in props.get("ATTENDEE", []):
        cutype = params.get("CUTYPE", ["INDIVIDUAL"])[0].upper()
        if cutype in ("RESOURCE", "ROOM"):
            continue
        email = _mailto(value)
        name = (params.get("CN") or [""])[0].strip()
        if name.lower() == email:
            name = ""
        partstat = (params.get("PARTSTAT") or ["NEEDS-ACTION"])[0].upper()
        response = {"ACCEPTED": "accepted", "DECLINED": "declined", "TENTATIVE": "tentative"}.get(
            partstat, "needs_action")
        role = (params.get("ROLE") or ["REQ-PARTICIPANT"])[0].upper()
        out.append({"name": name, "email": email, "response": response,
                    "self": email in self_emails, "optional": role == "OPT-PARTICIPANT"})
    return out


def _organizer(props: dict):
    v = _first(props, "ORGANIZER")
    if not v:
        return None
    params, value = v
    return {"name": (params.get("CN") or [""])[0].strip(), "email": _mailto(value)}


def _full_url(text: str, start: int) -> str:
    """The whole URL token beginning at `start` in `text` -- query string
    and all, so a Zoom `?pwd=` passcode or Teams/Webex context survives --
    minus any trailing punctuation the surrounding prose supplied."""
    m = _URL_RE.match(text, start)
    return m.group(0).rstrip(".,;:)>'\"") if m else ""


def conference_from_text(*texts) -> dict | None:
    """{"kind", "url", "code"} for the first meeting link found in the given
    strings (X-GOOGLE-CONFERENCE, LOCATION, DESCRIPTION, URL...). `code` is
    the normalized id the matcher keys on; `url` is the complete link as
    written (query string included), which is what a join must open."""
    for t in texts:
        if not t:
            continue
        m = _MEET_URL_RE.search(t)
        if m:
            return {"kind": "meet", "url": _full_url(t, m.start()) or m.group(0), "code": m.group(1).lower()}
    for t in texts:
        if not t:
            continue
        m = _ZOOM_URL_RE.search(t)
        if m:
            return {"kind": "zoom", "url": _full_url(t, m.start()) or m.group(0), "code": m.group(1)}
    for t in texts:
        if not t:
            continue
        m = _TEAMS_URL_RE.search(t)
        if m:
            return {"kind": "teams", "url": _full_url(t, m.start()) or m.group(0), "code": ""}
    for t in texts:
        if not t:
            continue
        m = _WEBEX_URL_RE.search(t)
        if m:
            return {"kind": "webex", "url": _full_url(t, m.start()) or m.group(0), "code": ""}
    return None


_FOCUS_RE = re.compile(r"^\s*(focus(\s+time)?|deep work|no meetings?)\s*$", re.I)
_OOO_RE = re.compile(r"^\s*(out of (the )?office|ooo|pto|vacation|holiday|sick( day)?)\b", re.I)
_WORKING_LOCATION_RE = re.compile(r"^\s*(working (from|location|remotely)|wfh|in (the )?office|at home)\b", re.I)


def event_kind(title: str, props: dict | None = None) -> str:
    """"default", or one of the kinds the matcher drops: "focus",
    "out_of_office", "working_location", "birthday". Google's ICS feed has no
    explicit event-type field, so this reads the title and Outlook's busy
    status."""
    if props:
        busy = _text(props, "X-MICROSOFT-CDO-BUSYSTATUS").upper()
        if busy == "OOF":
            return "out_of_office"
    t = title or ""
    if _FOCUS_RE.match(t):
        return "focus"
    if _OOO_RE.match(t):
        return "out_of_office"
    if _WORKING_LOCATION_RE.match(t):
        return "working_location"
    if re.search(r"\bbirthday\b", t, re.I):
        return "birthday"
    return "default"


def _raw_event(comp: dict, self_emails: set) -> dict | None:
    """One parsed VEVENT -> a normalized master record (start/end as
    date/datetime objects, plus the recurrence fields expand() needs)."""
    props = comp["props"]
    uid = _text(props, "UID")
    ds = _first(props, "DTSTART")
    if not uid or not ds:
        return None
    try:
        start = _parse_dt(ds[1], ds[0])
    except ValueError:
        return None
    all_day = not isinstance(start, datetime)
    de = _first(props, "DTEND")
    du = _first(props, "DURATION")
    try:
        if de:
            end = _parse_dt(de[1], de[0])
        elif du:
            end = start + _parse_duration(du[1])
        else:
            end = start + (timedelta(days=1) if all_day else timedelta(hours=1))
    except ValueError:
        end = start + (timedelta(days=1) if all_day else timedelta(hours=1))
    if isinstance(end, datetime) != isinstance(start, datetime):
        end = start + (timedelta(days=1) if all_day else timedelta(hours=1))
    if end <= start:
        end = start + (timedelta(days=1) if all_day else timedelta(minutes=1))

    title = _text(props, "SUMMARY") or "(untitled)"
    description = _text(props, "DESCRIPTION")
    location = _text(props, "LOCATION")
    url = _text(props, "URL")
    conf = conference_from_text(_text(props, "X-GOOGLE-CONFERENCE"), location, url,
                                _text(props, "X-MICROSOFT-SKYPETEAMSMEETINGURL"), description)
    attendees = _attendees(props, self_emails)
    organizer = _organizer(props)
    my_response = ""
    for a in attendees:
        if a["self"]:
            my_response = a["response"]
            break
    if not my_response and organizer and organizer["email"] in self_emails:
        my_response = "accepted"
    status = _text(props, "STATUS").lower() or "confirmed"
    transp = _text(props, "TRANSP").upper()
    if transp != "TRANSPARENT" and _text(props, "X-MICROSOFT-CDO-BUSYSTATUS").upper() == "FREE":
        transp = "TRANSPARENT"

    exdates = set()
    for params, value in props.get("EXDATE", []):
        for piece in value.split(","):
            try:
                exdates.add(_key(_parse_dt(piece, params)))
            except ValueError:
                pass
    rid = _first(props, "RECURRENCE-ID")
    recurrence_id = None
    if rid:
        try:
            recurrence_id = _parse_dt(rid[1], rid[0])
        except ValueError:
            recurrence_id = None
    rrule = _text(props, "RRULE")
    return {
        "uid": uid,
        "title": title,
        "start": start,
        "end": end,
        "all_day": all_day,
        "status": status if status in ("confirmed", "tentative", "cancelled") else "confirmed",
        "transparency": "transparent" if transp == "TRANSPARENT" else "opaque",
        "kind": event_kind(title, props),
        "my_response": my_response,
        "organizer": organizer,
        "attendees": attendees,
        "conference": conf,
        "location": location,
        "description": description[:MAX_DESCRIPTION_CHARS],
        "rrule": rrule,
        "exdates": exdates,
        "recurrence_id": recurrence_id,
    }


def detect_self_emails(comps: list, configured: str = "") -> set:
    """The calendar owner's address: `calendar_my_email` when set, else the
    address that appears on the most ATTENDEE lines across the feed (the
    owner is on nearly every invite they receive), when it clearly
    dominates AND nobody ties it. A tie (a feed that is mostly 1:1s with
    the same person) is left unknown rather than picked at random -- the
    wrong guess would flip who "self" is for speaker naming and read the
    other person's replies as yours. Empty means unknown."""
    if configured.strip():
        return {configured.strip().lower()}
    counts: dict = {}
    events_with = 0
    for comp in comps:
        seen = set()
        for _params, value in comp["props"].get("ATTENDEE", []):
            seen.add(_mailto(value))
        if seen:
            events_with += 1
            for e in seen:
                counts[e] = counts.get(e, 0) + 1
    if not counts:
        return set()
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    top, n = ranked[0]
    if len(ranked) > 1 and ranked[1][1] == n:
        return set()  # a tie: no owner without calendar_my_email
    if events_with >= 3 and n >= 0.5 * events_with:
        return {top}
    return set()


# ---------------------------------------------------------------- recurrence

def parse_rrule(value: str) -> dict:
    rule: dict = {}
    for part in value.split(";"):
        k, _, v = part.partition("=")
        k = k.strip().upper()
        v = v.strip()
        if not k:
            continue
        if k in ("INTERVAL", "COUNT"):
            try:
                rule[k] = max(1, int(v))
            except ValueError:
                pass
        elif k == "BYDAY":
            days = []
            for tok in v.upper().split(","):
                m = re.match(r"^([+-]?\d+)?(MO|TU|WE|TH|FR|SA|SU)$", tok.strip())
                if m:
                    days.append((int(m.group(1)) if m.group(1) else None, _WEEKDAYS[m.group(2)]))
            rule[k] = days
        elif k in ("BYMONTHDAY", "BYMONTH", "BYSETPOS"):
            vals = []
            for tok in v.split(","):
                try:
                    n = int(tok)
                except ValueError:
                    continue
                if n != 0:
                    vals.append(n)
            rule[k] = vals
        elif k == "WKST":
            rule[k] = _WEEKDAYS.get(v.upper(), 0)
        else:
            rule[k] = v.upper() if k == "FREQ" else v
    return rule


# What expand_rrule() actually implements. Anything outside this is refused
# for the whole series (expand() skips it and counts it) rather than
# expanded wrongly: a made-up instance can confidently match a recording
# and hand it the wrong title and attendees.
_RRULE_PARTS = {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "BYMONTHDAY", "BYMONTH", "BYSETPOS", "WKST"}


def unsupported_rrule_reason(rule: dict) -> str:
    """"" when expand_rrule() handles every part of `rule` exactly, else why
    not (the offending part), for diagnostics."""
    freq = rule.get("FREQ", "")
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        return f"FREQ={freq or '?'}"
    for k in rule:
        if k not in _RRULE_PARTS:
            return k  # BYWEEKNO, BYYEARDAY, BYHOUR, BYMINUTE, BYSECOND, RSCALE, X-…
    ordinals = [o for o, _wd in rule.get("BYDAY", []) if o is not None]
    if freq in ("DAILY", "WEEKLY"):
        for k in ("BYMONTHDAY", "BYMONTH", "BYSETPOS"):
            if rule.get(k):
                return f"{k} with FREQ={freq}"
        if ordinals:
            return f"ordinal BYDAY with FREQ={freq}"
    if freq == "YEARLY" and ordinals and not rule.get("BYMONTH"):
        # "20MO" in a YEARLY rule is the 20th Monday of the YEAR, not of a month.
        return "ordinal BYDAY with FREQ=YEARLY and no BYMONTH"
    if rule.get("BYSETPOS") and not (rule.get("BYDAY") or rule.get("BYMONTHDAY")):
        return "BYSETPOS without BYDAY/BYMONTHDAY"
    return ""


def _setpos(days: list, positions: list) -> list:
    """RFC 5545 BYSETPOS over one occurrence set (sorted): the n-th (1-based)
    or -n-th from the end."""
    picked = set()
    for p in positions:
        if p > 0 and p <= len(days):
            picked.add(days[p - 1])
        elif p < 0 and -p <= len(days):
            picked.add(days[p])
    return sorted(picked)


def _until(rule: dict, dtstart):
    raw = rule.get("UNTIL")
    if not raw:
        return None
    try:
        u = _parse_dt(raw, {})
    except ValueError:
        return None
    if isinstance(dtstart, datetime):
        if not isinstance(u, datetime):
            u = datetime(u.year, u.month, u.day, 23, 59, 59, tzinfo=dtstart.tzinfo)
        elif u.tzinfo is None:
            u = u.replace(tzinfo=dtstart.tzinfo)
        return u
    return u if not isinstance(u, datetime) else u.date()


def _nth_weekday(year: int, month: int, weekday: int, ordinal) -> list:
    """Days of the month falling on `weekday`: the ordinal-th (negative from
    the end) or, with ordinal None, all of them."""
    first = date(year, month, 1)
    days = []
    d = first + timedelta(days=(weekday - first.weekday()) % 7)
    while d.month == month:
        days.append(d.day)
        d += timedelta(days=7)
    if ordinal is None:
        return days
    if ordinal > 0:
        return [days[ordinal - 1]] if ordinal <= len(days) else []
    return [days[ordinal]] if -ordinal <= len(days) else []


def _month_days(year: int, month: int, rule: dict, dtstart, setpos: bool = True) -> list:
    """Day numbers a MONTHLY/YEARLY rule selects inside one month: BYDAY,
    BYMONTHDAY, or (RFC 5545) their intersection when both are given, then
    BYSETPOS over that month's set (unless the caller applies it over a
    larger set, as YEARLY does)."""
    import calendar as _cal
    last = _cal.monthrange(year, month)[1]
    days = None
    if rule.get("BYDAY"):
        days = set()
        for ordinal, wd in rule["BYDAY"]:
            days.update(_nth_weekday(year, month, wd, ordinal))
    if rule.get("BYMONTHDAY"):
        by_md = set()
        for md in rule["BYMONTHDAY"]:
            d = md if md > 0 else last + 1 + md
            if 1 <= d <= last:
                by_md.add(d)
        days = by_md if days is None else days & by_md
    if days is None:
        return [dtstart.day] if dtstart.day <= last else []
    out = sorted(days)
    if setpos and rule.get("BYSETPOS"):
        out = _setpos(out, rule["BYSETPOS"])
    return out


def expand_rrule(dtstart, rule: dict, exdates: set, win_start: datetime, win_end: datetime,
                 duration: timedelta):
    """Instance starts of `rule` (same type as dtstart) whose span touches
    [win_start, win_end]. Wall-clock arithmetic in dtstart's zone, so a 9 AM
    weekly stays 9 AM across DST. COUNT counts every occurrence, exdated or
    not (RFC 5545); UNTIL is inclusive. Bounded by MAX_RULE_ITERATIONS."""
    freq = rule.get("FREQ", "")
    interval = rule.get("INTERVAL", 1)
    count = rule.get("COUNT")
    until = _until(rule, dtstart)
    is_dt = isinstance(dtstart, datetime)
    lo = win_start - duration
    hi = win_end

    def past_until(occ) -> bool:
        if until is None:
            return False
        if is_dt:
            return occ > until
        return occ > until

    def in_window(occ) -> bool:
        occ_dt = _as_datetime(occ)
        return lo <= occ_dt <= hi

    produced = 0
    iterations = 0
    out = []

    def consider(occ) -> bool:
        """Records `occ` when it falls in the window. Returns False when
        generation must stop: past UNTIL, past COUNT, or past the window
        (no later occurrence can matter, COUNT or not)."""
        nonlocal produced
        if occ < dtstart:
            return True
        if past_until(occ) or _as_datetime(occ) > hi:
            return False
        if count is not None and produced >= count:
            return False
        produced += 1
        if _key(occ) in exdates:
            return True
        if in_window(occ):
            out.append(occ)
        return True

    if freq == "DAILY":
        step = timedelta(days=interval)
        occ = dtstart
        if count is None:
            skip = int((lo - _as_datetime(dtstart)) / step) - 2
            if skip > 0:
                occ = dtstart + step * skip
        bydays = {wd for _o, wd in rule.get("BYDAY", [])}
        while iterations < MAX_RULE_ITERATIONS:
            iterations += 1
            if bydays and occ.weekday() not in bydays:
                if past_until(occ) or _as_datetime(occ) > hi:
                    break
                occ += step
                continue
            if not consider(occ):
                break
            occ += step
        return out

    if freq == "WEEKLY":
        wkst = rule.get("WKST", 0)
        days = {wd for _o, wd in rule.get("BYDAY", [])} or {dtstart.weekday()}
        # In week order from WKST, so occurrences come out sorted and COUNT
        # counts them in the right sequence.
        offsets = sorted((wd - wkst) % 7 for wd in days)
        week0 = dtstart - timedelta(days=(dtstart.weekday() - wkst) % 7)
        step = timedelta(days=7 * interval)
        k = 0
        if count is None:
            skip = int((lo - _as_datetime(week0)) / step) - 1
            if skip > 0:
                k = skip
        stop = False
        while not stop and iterations < MAX_RULE_ITERATIONS:
            iterations += 1
            base = week0 + step * k
            if _as_datetime(base) > hi:
                break
            for off in offsets:
                if not consider(base + timedelta(days=off)):
                    stop = True
                    break
            k += 1
        return out

    if freq in ("MONTHLY", "YEARLY"):
        months_step = interval if freq == "MONTHLY" else 12 * interval
        y, m = dtstart.year, dtstart.month
        by_month = sorted(mo for mo in (rule.get("BYMONTH") or []) if 1 <= mo <= 12)
        # YEARLY: BYSETPOS ranks the whole year's set (every BYMONTH month
        # together, RFC 5545); MONTHLY: each month is its own set. A YEARLY
        # rule with BYMONTHDAY or BYDAY but no BYMONTH means every month of
        # the year (RFC 5545: those parts expand); only a bare FREQ=YEARLY
        # (or one with just BYMONTH) sticks to dtstart's month.
        yearly_setpos = freq == "YEARLY" and bool(rule.get("BYSETPOS"))
        if freq == "YEARLY" and not by_month and (rule.get("BYMONTHDAY") or rule.get("BYDAY")):
            by_month = list(range(1, 13))
        stop = False
        while not stop and iterations < MAX_RULE_ITERATIONS:
            iterations += 1
            # The window guard that doesn't depend on any occurrence being
            # produced: a rule whose set is empty every period (an
            # intersection with nothing in it, a BYSETPOS past the set's
            # size) must still stop at the window's end.
            if y > 9999 or _as_datetime(date(y, m if freq == "MONTHLY" else 1, 1)) - timedelta(days=1) > hi:
                break
            if freq == "MONTHLY" and by_month and m not in by_month:
                if _as_datetime(date(y, m, 1)) - timedelta(days=1) > hi:
                    break
            else:
                occs = []
                for mo in ([m] if freq == "MONTHLY" else (by_month or [m])):
                    if _as_datetime(date(y, mo, 1)) - timedelta(days=1) > hi and not yearly_setpos:
                        stop = True
                        break
                    for day in _month_days(y, mo, rule, dtstart, setpos=not yearly_setpos):
                        occs.append(dtstart.replace(year=y, month=mo, day=day))
                if yearly_setpos:
                    occs = _setpos(sorted(occs), rule["BYSETPOS"])
                for occ in occs:
                    if not consider(occ):
                        stop = True
                        break
            total = y * 12 + (m - 1) + months_step
            y, m = total // 12, total % 12 + 1
        return out

    # Unknown FREQ (SECONDLY/MINUTELY/HOURLY are never meetings): just the
    # first instance.
    consider(dtstart)
    return out


def _instance(master: dict, start, end, recurring: bool, recurrence_id=None) -> dict:
    ev = {k: v for k, v in master.items() if k not in ("rrule", "exdates", "recurrence_id")}
    ev["start"] = to_iso(start)
    ev["end"] = to_iso(end)
    ev["recurring"] = recurring
    ev["id"] = master["uid"] if not recurring else f"{master['uid']}/{to_iso(recurrence_id or start)}"
    ev["recurrence_id"] = to_iso(recurrence_id) if recurrence_id is not None else ""
    return ev


def expand(raw_events: list, win_start: datetime, win_end: datetime, diag: dict | None = None) -> list:
    """Masters + RRULEs + RECURRENCE-ID overrides -> concrete instances whose
    span touches [win_start, win_end], as JSON-ready dicts (see CONTRACT.md).
    An override replaces the instance its RECURRENCE-ID names (wherever that
    override moved it); a canceled override removes it. A series whose RRULE
    uses a part the expander doesn't implement is skipped whole (its moved
    overrides, being concrete events, still count) and counted in `diag`
    (`rules_skipped`, `skipped`: [{"uid", "rule", "reason"}])."""
    overrides: dict = {}
    masters = []
    for ev in raw_events:
        if ev["recurrence_id"] is not None:
            overrides.setdefault(ev["uid"], {})[_key(ev["recurrence_id"])] = ev
        else:
            masters.append(ev)
    out = []
    seen_override_keys: set = set()
    for ev in masters:
        duration = _as_datetime(ev["end"]) - _as_datetime(ev["start"])
        ov = overrides.get(ev["uid"], {})
        if ev["rrule"]:
            rule = parse_rrule(ev["rrule"])
            reason = unsupported_rrule_reason(rule)
            if reason:
                if diag is not None:
                    diag["rules_skipped"] = diag.get("rules_skipped", 0) + 1
                    diag.setdefault("skipped", []).append({"uid": ev["uid"], "rule": ev["rrule"], "reason": reason})
                continue
            starts = expand_rrule(ev["start"], rule, ev["exdates"], win_start, win_end, duration)
            for s in starts:
                k = _key(s)
                if k in ov:
                    seen_override_keys.add((ev["uid"], k))
                    o = ov[k]
                    if o["status"] == "cancelled":
                        continue
                    if _as_datetime(o["end"]) >= win_start and _as_datetime(o["start"]) <= win_end:
                        out.append(_instance(o, o["start"], o["end"], True, o["recurrence_id"]))
                    continue
                out.append(_instance(ev, s, s + duration, True))
        else:
            s, e = _as_datetime(ev["start"]), _as_datetime(ev["end"])
            if e >= win_start and s <= win_end:
                out.append(_instance(ev, ev["start"], ev["end"], False))
    # Overrides whose original slot fell outside the window but that were
    # moved into it (or whose master is missing from the feed).
    for uid, table in overrides.items():
        for k, o in table.items():
            if (uid, k) in seen_override_keys or o["status"] == "cancelled":
                continue
            if _as_datetime(o["end"]) >= win_start and _as_datetime(o["start"]) <= win_end:
                out.append(_instance(o, o["start"], o["end"], True, o["recurrence_id"]))
    out.sort(key=lambda e: (from_iso(e["start"]), e["title"]))
    return out


def events_from_ics(text: str, win_start: datetime, win_end: datetime, my_email: str = "",
                    diag: dict | None = None) -> list:
    """Events in the window from an iCalendar text. `diag`, when given, gets
    `self_email` (the owner's address, "" when unknown), `self_known`, and
    expand()'s skipped-rule counts."""
    comps = parse_ics(text)
    self_emails = detect_self_emails(comps, my_email)
    if diag is not None:
        diag["self_email"] = next(iter(sorted(self_emails)), "")
        diag["self_known"] = bool(self_emails)
    raw = [r for r in (_raw_event(c, self_emails) for c in comps) if r]
    return expand(raw, win_start, win_end, diag)


# ---------------------------------------------------------------- calendar_command events

def normalize_external(d: dict) -> dict | None:
    """One event from `calendar_command` output -> the normalized shape.
    Lenient: only title/start/end are required; times may be ISO 8601 (with
    or without offset -- naive means local) or epoch seconds; attendees may
    be dicts, "Name <email>" strings, or bare names."""
    if not isinstance(d, dict):
        return None
    title = str(d.get("title") or d.get("summary") or "").strip()
    if not title or d.get("start") in (None, ""):
        return None
    try:
        start = from_iso(d["start"])
        end = from_iso(d["end"]) if d.get("end") not in (None, "") else start + timedelta(hours=1)
    except (ValueError, TypeError):
        return None
    all_day = bool(d.get("all_day", False))
    if isinstance(d.get("start"), str) and re.match(r"^\d{4}-\d{2}-\d{2}$", d["start"].strip()):
        all_day = True
    attendees = []
    for a in d.get("attendees") or []:
        if isinstance(a, str):
            m = re.match(r"^\s*(.*?)\s*<([^>]+)>\s*$", a)
            attendees.append({"name": m.group(1) if m else a.strip(),
                              "email": m.group(2).lower() if m else "",
                              "response": "needs_action", "self": False, "optional": False})
        elif isinstance(a, dict):
            resp = str(a.get("response") or a.get("responseStatus") or "needs_action").lower()
            resp = {"needsaction": "needs_action", "needs-action": "needs_action"}.get(resp, resp)
            if resp not in ("accepted", "declined", "tentative", "needs_action"):
                resp = "needs_action"
            attendees.append({"name": str(a.get("name") or a.get("displayName") or "").strip(),
                              "email": str(a.get("email") or "").strip().lower(),
                              "response": resp, "self": bool(a.get("self", False)),
                              "optional": bool(a.get("optional", False))})
    organizer = d.get("organizer")
    if isinstance(organizer, str):
        organizer = {"name": organizer, "email": ""}
    elif isinstance(organizer, dict):
        organizer = {"name": str(organizer.get("name") or organizer.get("displayName") or ""),
                     "email": str(organizer.get("email") or "").lower()}
    else:
        organizer = None
    conf = d.get("conference")
    if isinstance(conf, str):
        conf = conference_from_text(conf)
    elif isinstance(conf, dict):
        conf = conference_from_text(str(conf.get("url") or "")) or {
            "kind": str(conf.get("kind") or "other"), "url": str(conf.get("url") or ""),
            "code": str(conf.get("code") or "")}
    else:
        conf = conference_from_text(str(d.get("hangoutLink") or ""), str(d.get("location") or ""),
                                    str(d.get("description") or ""))
    my_response = str(d.get("my_response") or "").lower()
    if not my_response:
        for a in attendees:
            if a["self"]:
                my_response = a["response"]
    status = str(d.get("status") or "confirmed").lower()
    transparency = str(d.get("transparency") or "opaque").lower()
    kind = str(d.get("kind") or d.get("eventType") or "").lower()
    kind = {"focustime": "focus", "outofoffice": "out_of_office", "workinglocation": "working_location",
            "": ""}.get(kind, kind)
    if not kind:
        kind = event_kind(title)
    uid = str(d.get("uid") or d.get("id") or f"{title}@{to_iso(start)}")
    return {
        "id": str(d.get("id") or uid),
        "uid": uid,
        "title": title,
        "start": to_iso(start.date() if all_day else start),
        "end": to_iso(end.date() if all_day else end),
        "all_day": all_day,
        "status": status if status in ("confirmed", "tentative", "cancelled") else "confirmed",
        "transparency": "transparent" if transparency == "transparent" else "opaque",
        "kind": kind,
        "my_response": my_response,
        "organizer": organizer,
        "attendees": attendees,
        "conference": conf,
        "location": str(d.get("location") or ""),
        "description": str(d.get("description") or "")[:MAX_DESCRIPTION_CHARS],
        "recurring": bool(d.get("recurring", False)),
        "recurrence_id": str(d.get("recurrence_id") or ""),
    }


# ---------------------------------------------------------------- sources

def source_kind(cfg: dict) -> str:
    """"ics", "command", or "off" (calendar_enabled false, or nothing configured)."""
    src = str(cfg.get("calendar_source") or "ics")
    if src == "command":
        return "command" if str(cfg.get("calendar_command") or "").strip() else "off"
    return "ics" if config.secret_status(cfg, "calendar_ics_url")["set"] else "off"


def _cache_dir() -> Path:
    return config.STATE_DIR / "calendar"


def _cache_paths() -> tuple:
    d = _cache_dir()
    return d / "feed.ics", d / "feed.json"


def _read_cache_meta() -> dict:
    _feed, meta = _cache_paths()
    try:
        data = json.loads(meta.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _url_key(url: str) -> str:
    """A fingerprint of the feed URL so a changed address invalidates the
    cache -- never the URL itself (it is the credential)."""
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def _feed_url(cfg: dict) -> str:
    """The configured feed address as given (env, config, or its command)."""
    return config.secret(cfg, "calendar_ics_url", config.SECRET_ENV.get("calendar_ics_url", ""))


def _normalize_feed_url(url: str) -> str | None:
    """webcal:// -> https://; None unless the result is an http(s) URL with
    a host. Decided by us, not by urllib, so no exception ever carries the
    address."""
    u = url.strip()
    if u.lower().startswith("webcal://"):
        u = "https://" + u[len("webcal://"):]
    m = re.match(r"^(https?)://([^/?#\s]+)", u, flags=re.I)
    if not m or not m.group(2):
        return None
    return u


def _redact(text: str, *urls: str) -> str:
    """Strips the feed address -- in every form it could take: as given,
    normalized, with or without its scheme -- from an error message."""
    forms = set()
    for u in urls:
        u = (u or "").strip()
        if not u:
            continue
        forms.add(u)
        for prefix in ("https://", "http://", "webcal://"):
            if u.lower().startswith(prefix):
                forms.add(u[len(prefix):])
    for form in sorted(forms, key=len, reverse=True):
        if len(form) >= 8:  # never blank a trivially short token out of unrelated text
            text = text.replace(form, "<feed address>")
    return text


def fetch_ics(cfg: dict, refresh: bool = False, now: float | None = None) -> tuple:
    """(ics_text | None, info). Serves the on-disk cache while it is younger
    than `calendar_cache_ttl_s`; otherwise fetches (with ETag/Last-Modified
    revalidation) and falls back to the stale copy on any failure. `info`:
    {"source": "ics", "cached": bool, "fetched_at": epoch, "error": ""}."""
    now = time.time() if now is None else now
    info = {"source": "ics", "cached": False, "fetched_at": 0, "error": "", "bytes": 0}
    raw_url = _feed_url(cfg)
    if not raw_url:
        info["error"] = "no calendar feed address set"
        return None, info
    url = _normalize_feed_url(raw_url)
    if url is None:
        # Validated here, before anything can raise with the address inside
        # it: urllib's own "unknown url type: '<the whole address>'" would
        # carry the credential into `calendar test --json` and .meta.json.
        info["error"] = "feed address must start with https:// or webcal://"
        return None, info
    feed_path, meta_path = _cache_paths()
    meta = _read_cache_meta()
    key = _url_key(url)
    ttl = float(cfg.get("calendar_cache_ttl_s") or 900)
    have_cache = feed_path.exists() and meta.get("key") == key

    def serve_cache(error: str = ""):
        try:
            text = feed_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            info["error"] = error or f"calendar cache unreadable ({e.__class__.__name__})"
            return None, info
        info.update(cached=True, fetched_at=meta.get("fetched_at", 0), bytes=len(text), error=error)
        return text, info

    if have_cache and not refresh and now - float(meta.get("fetched_at") or 0) < ttl:
        return serve_cache()

    headers = {"User-Agent": "Spitball/2 (+calendar)", "Accept": "text/calendar, */*;q=0.5"}
    if have_cache:
        if meta.get("etag"):
            headers["If-None-Match"] = meta["etag"]
        if meta.get("last_modified"):
            headers["If-Modified-Since"] = meta["last_modified"]
    # Every error message below is passed through _redact(): the address is
    # the credential and must never appear in `info["error"]`, which ends up
    # in `calendar test --json` and every recording's .meta.json.
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:
            body = resp.read()
            etag = resp.headers.get("ETag", "")
            last_modified = resp.headers.get("Last-Modified", "")
    except urllib.error.HTTPError as e:
        if e.code == 304 and have_cache:
            meta["fetched_at"] = now
            config.atomic_write(meta_path, json.dumps(meta), mode=0o600)
            return serve_cache()
        err = f"calendar feed: HTTP {e.code}"
        if e.code in (401, 403, 404):
            err += " (the secret address may have been reset -- copy a fresh one from your calendar's settings)"
        return serve_cache(err) if have_cache else (None, {**info, "error": err})
    except ValueError as e:  # a malformed address urllib refused (e.g. http.client.InvalidURL)
        err = _redact(f"feed address is not a valid URL ({e.__class__.__name__})", raw_url, url)
        return serve_cache(err) if have_cache else (None, {**info, "error": err})
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        reason = getattr(e, "reason", None) or e.__class__.__name__
        err = _redact(f"calendar feed unreachable ({reason})", raw_url, url)
        return serve_cache(err) if have_cache else (None, {**info, "error": err})
    text = body.decode("utf-8", errors="replace")
    if "BEGIN:VCALENDAR" not in text[:2000].upper():
        err = "calendar feed did not return an iCalendar file"
        return serve_cache(err) if have_cache else (None, {**info, "error": err})
    _cache_dir().mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(_cache_dir(), 0o700)
    except OSError:
        pass
    config.atomic_write(feed_path, text, mode=0o600)
    meta = {"key": key, "fetched_at": now, "etag": etag, "last_modified": last_modified, "bytes": len(text)}
    config.atomic_write(meta_path, json.dumps(meta), mode=0o600)
    info.update(cached=False, fetched_at=now, bytes=len(text))
    return text, info


def run_command(cfg: dict, win_start: datetime, win_end: datetime) -> tuple:
    """(events, info) from `calendar_command`. The window is passed in the
    environment as SPITBALL_WINDOW_START / SPITBALL_WINDOW_END (ISO 8601)."""
    info = {"source": "command", "cached": False, "fetched_at": time.time(), "error": ""}
    cmd = str(cfg.get("calendar_command") or "").strip()
    if not cmd:
        info["error"] = "no calendar_command set"
        return [], info
    env = dict(os.environ)
    env["SPITBALL_WINDOW_START"] = to_iso(win_start)
    env["SPITBALL_WINDOW_END"] = to_iso(win_end)
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=COMMAND_TIMEOUT_S, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        info["error"] = f"calendar_command failed to run ({e.__class__.__name__})"
        return [], info
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()
        info["error"] = f"calendar_command exited {r.returncode}" + (f": {tail[-1][:160]}" if tail else "")
        return [], info
    try:
        data = json.loads(r.stdout or "[]")
    except ValueError:
        info["error"] = "calendar_command printed something that isn't JSON"
        return [], info
    if isinstance(data, dict):
        data = data.get("events") or data.get("items") or []
    if not isinstance(data, list):
        info["error"] = "calendar_command must print a JSON array of events"
        return [], info
    events = [e for e in (normalize_external(d) for d in data) if e]
    events = [e for e in events if from_iso(e["end"]) >= win_start and from_iso(e["start"]) <= win_end]
    events.sort(key=lambda e: (from_iso(e["start"]), e["title"]))
    return events, info


def load_events(cfg: dict, win_start: datetime, win_end: datetime, refresh: bool = False) -> tuple:
    """(events in the window, info) from whichever source is configured."""
    kind = source_kind(cfg)
    if kind == "command":
        return run_command(cfg, win_start, win_end)
    if kind == "ics":
        text, info = fetch_ics(cfg, refresh=refresh)
        if text is None:
            return [], info
        diag: dict = {}
        try:
            events = events_from_ics(text, win_start, win_end, str(cfg.get("calendar_my_email") or ""), diag)
        except Exception as e:  # a feed we can't parse is a source error, never a crash
            info["error"] = _redact(f"couldn't parse the calendar feed ({e.__class__.__name__}: {e})", _feed_url(cfg))
            return [], info
        info["self_email"] = diag.get("self_email", "")
        info["self_known"] = bool(diag.get("self_known"))
        info["rules_skipped"] = int(diag.get("rules_skipped", 0))
        info["skipped"] = diag.get("skipped", [])
        return events, info
    return [], {"source": "off", "cached": False, "fetched_at": 0, "error": ""}


# ---------------------------------------------------------------- window titles

def window_meet_codes(run=subprocess.run) -> list:
    """Google Meet codes (abc-defg-hij) visible in any window title right
    now, via `hyprctl clients -j` -- a read-only query. Chromium titles a
    Meet tab "Meet – abc-defg-hij", which is the one exact key a call
    offers. Zoom's Linux client titles its window "Zoom Meeting" (no id), so
    nothing else is harvested; window titles themselves are never stored."""
    try:
        r = run(["hyprctl", "clients", "-j"], capture_output=True, text=True, timeout=HYPRCTL_TIMEOUT_S)
        clients = json.loads(r.stdout or "[]")
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    codes = []
    for c in clients if isinstance(clients, list) else []:
        title = str(c.get("title", "")) if isinstance(c, dict) else ""
        for code in MEET_CODE_RE.findall(title.lower()):
            if code not in codes:
                codes.append(code)
    return codes


# ---------------------------------------------------------------- matcher

def _bounds(ev: dict) -> tuple:
    return from_iso(ev["start"]), from_iso(ev["end"])


def hard_filter(ev: dict) -> str:
    """Why an event can never be the call -- "" when it may be."""
    if ev.get("all_day"):
        return "all-day"
    if ev.get("status") == "cancelled":
        return "cancelled"
    if ev.get("transparency") == "transparent":
        return "marked free"
    kind = ev.get("kind") or "default"
    if kind != "default":
        return {"focus": "focus time", "out_of_office": "out of office",
                "working_location": "working location", "birthday": "birthday"}.get(kind, kind)
    if ev.get("my_response") == "declined":
        return "declined"
    return ""


def _app_key(app: str) -> str:
    return (app or "").strip().lower()


def score_event(ev: dict, started_at: float, duration: float | None, meet_codes, app: str) -> tuple:
    """(score, reasons) per docs/SPEC-v2.md §2 / the calendar research §4."""
    start, end = _bounds(ev)
    t = datetime.fromtimestamp(started_at, tz=timezone.utc)
    score = 0
    reasons = []
    conf = ev.get("conference") or {}
    kind = conf.get("kind", "")
    code = (conf.get("code") or "").lower()
    codes = [c.lower() for c in (meet_codes or [])]
    code_conflict = False
    if kind == "meet" and code and codes:
        if code in codes:
            score += 100
            reasons.append("Meet code in a window title")
        else:
            score -= 50
            code_conflict = True
            reasons.append("a different Meet code is open")
    a = _app_key(app)
    if a:
        if code_conflict:
            pass  # the browser is on another meeting; no host credit
        elif kind == "meet" and any(b in a for b in _BROWSERS):
            score += 30
            reasons.append("Meet link, browser on the mic")
        elif kind in _NATIVE.values() and _NATIVE.get(a) == kind:
            score += 30
            reasons.append(f"{kind} link, {app} on the mic")
        elif kind in ("teams", "webex", "zoom") and any(b in a for b in _BROWSERS):
            score += 15
            reasons.append(f"{kind} link, browser on the mic")
        elif not kind and any(x in a for x in _NO_CONFERENCE_APPS):
            score += 30
            reasons.append(f"no meeting link, {app} on the mic")
        elif kind and a in _NATIVE and _NATIVE[a] != kind:
            score -= 30
            reasons.append(f"{kind} link but {app} on the mic")
    if start <= t <= end:
        score += 20
        reasons.append("started during the event")
    others = [x for x in ev.get("attendees") or []
              if not x.get("self") and x.get("response") != "declined"]
    n = min(3, len(others))
    if n:
        score += 10 * n
        reasons.append(f"{len(others)} other attendee{'s' if len(others) != 1 else ''}")
    resp = ev.get("my_response") or ""
    if resp == "accepted":
        score += 10
        reasons.append("accepted")
    elif resp == "needs_action":
        score += 5
    minutes_off = abs((t - start).total_seconds()) / 60
    if minutes_off > 5:
        penalty = min(30, int(round(1.5 * (minutes_off - 5))))
        score -= penalty
        reasons.append(f"started {int(round(minutes_off))} min from the event start")
    if duration:
        rec_end = t + timedelta(seconds=duration)
        overlap = (min(end, rec_end) - max(start, t)).total_seconds()
        frac = max(0.0, overlap) / duration
        bonus = int(round(20 * min(1.0, frac)))
        if bonus:
            score += bonus
            reasons.append(f"{int(round(100 * min(1.0, frac)))}% of the recording inside the event")
    return score, reasons


def candidates_for(events: list, started_at: float) -> list:
    t = datetime.fromtimestamp(started_at, tz=timezone.utc)
    out = []
    for ev in events:
        try:
            start, end = _bounds(ev)
        except (ValueError, KeyError, TypeError):
            continue
        if start - timedelta(seconds=WINDOW_BEFORE_S) <= t <= end + timedelta(seconds=WINDOW_AFTER_S):
            out.append(ev)
    return out


def match(events: list, started_at: float, duration: float | None = None, meet_codes=(), app: str = "") -> dict:
    """{"event": <normalized event> | None, "confident": bool, "confidence": int,
    "candidates": [{id, title, start, end, score, filtered, reasons}],
    "summary": "..."}. Deterministic: same inputs, same answer."""
    rows = []
    best = None
    scored = []
    for ev in candidates_for(events, started_at):
        why = hard_filter(ev)
        row = {"id": ev.get("id", ""), "title": ev.get("title", ""), "start": ev.get("start", ""),
               "end": ev.get("end", ""), "score": 0, "filtered": why, "reasons": []}
        if not why:
            s, reasons = score_event(ev, started_at, duration, meet_codes, app)
            row["score"], row["reasons"] = s, reasons
            scored.append((s, ev))
        rows.append(row)
    scored.sort(key=lambda x: (-x[0], from_iso(x[1]["start"])))
    rows.sort(key=lambda r: (r["filtered"] != "", -r["score"], r["start"]))
    confident = False
    confidence = 0
    if scored:
        top_score, top = scored[0]
        runner = scored[1][0] if len(scored) > 1 else None
        confidence = top_score
        if top_score >= MATCH_THRESHOLD and (runner is None or top_score - runner >= MATCH_MARGIN):
            best, confident = top, True
    live = sum(1 for r in rows if not r["filtered"])
    if confident:
        summary = f"matched “{best['title']}” (score {confidence})"
    elif scored:
        summary = f"no confident match ({live} candidate{'s' if live != 1 else ''}, best score {confidence})"
    elif rows:
        summary = f"no match ({len(rows)} event{'s' if len(rows) != 1 else ''} nearby, all filtered out)"
    else:
        summary = "no events at that time"
    return {"event": best if confident else None, "confident": confident, "confidence": confidence,
            "candidates": rows, "summary": summary}


# ---------------------------------------------------------------- daemon / process API

def snapshot(cfg: dict, started_at: float, app: str = "", meet_codes=None, refresh: bool = False) -> dict:
    """Everything the matcher will ever need for this call, captured once so
    `reprocess` and offline runs see the same picture (docs/SPEC-v2.md §2).
    Never raises. Reads window titles via hyprctl when `meet_codes` is None."""
    snap = {"source": "off", "fetched_at": 0, "cached": False, "error": "", "app": app or "",
            "started_at": float(started_at), "meet_codes": [], "events": [], "match": None}
    try:
        if meet_codes is None:
            meet_codes = window_meet_codes()
        snap["meet_codes"] = list(meet_codes)
        t = datetime.fromtimestamp(started_at, tz=timezone.utc)
        pad = timedelta(seconds=LOOKUP_PAD_S)
        events, info = load_events(cfg, t - pad, t + pad, refresh=refresh)
        snap.update(source=info.get("source", "off"), fetched_at=info.get("fetched_at", 0),
                    cached=bool(info.get("cached")), error=info.get("error", ""))
        if info.get("source") == "ics":
            # Diagnostics that travel with the call: whose calendar this is
            # (empty = couldn't tell; set calendar_my_email) and how many
            # recurring series were skipped for RRULE parts we don't expand.
            snap["self_email"] = str(info.get("self_email") or "")
            snap["self_known"] = bool(info.get("self_known"))
            snap["rules_skipped"] = int(info.get("rules_skipped") or 0)
        # Keep only what could ever be a candidate, so .meta.json stays small.
        snap["events"] = candidates_for(events, started_at)
    except Exception as e:  # pragma: no cover - belt and braces; nothing here may escape
        snap["error"] = _redact(f"calendar lookup failed ({e.__class__.__name__}: {e})", _feed_url(cfg))
    return snap


def decide(snap: dict, duration: float | None = None) -> dict:
    """The match for a snapshot, with the recording's duration folded in
    when known (so back-to-back events resolve toward the one the audio
    actually spanned). Honors a manual override (`reprocess --event/--no-event`)."""
    if not isinstance(snap, dict):
        return match([], 0)
    result = match(snap.get("events") or [], float(snap.get("started_at") or 0), duration,
                   snap.get("meet_codes") or [], snap.get("app") or "")
    override = snap.get("override")
    if isinstance(override, dict) and "event" in override:
        wanted = override["event"]
        if wanted is None:
            result.update(event=None, confident=False, summary="no event (set by hand)")
        else:
            # A reminder's Join & record pins the event it fired for and
            # keeps a copy under `pinned`, so the pin holds even if the feed
            # has since dropped or moved that occurrence.
            pool = list(snap.get("events") or [])
            pinned = snap.get("pinned")
            if isinstance(pinned, dict) and pinned.get("id") == wanted and \
                    not any(ev.get("id") == wanted for ev in pool):
                pool.append(pinned)
            how = "set by hand" if not (isinstance(pinned, dict) and pinned.get("id") == wanted) \
                else "joined from the reminder"
            for ev in pool:
                if ev.get("id") == wanted:
                    result.update(event=ev, confident=True, confidence=max(result["confidence"], 100),
                                  summary=f"matched “{ev['title']}” ({how})")
                    break
            else:
                result["summary"] += f"; override {wanted!r} is not among the candidates"
    return result


def for_call(meta: dict, cfg: dict, duration: float | None) -> dict | None:
    """process.process()'s entry point: the decision for one call, or None
    when the calendar is off. Takes the snapshot the daemon stored in
    .meta.json; if there is none (an old folder, or the lookup hadn't
    finished when the call ended) and the calendar is enabled now, looks the
    call up on the spot and stores that snapshot into `meta`. Never raises."""
    try:
        snap = meta.get("calendar")
        if not isinstance(snap, dict) or "events" not in snap:
            if not cfg.get("calendar_enabled") or source_kind(cfg) == "off":
                return None
            snap = snapshot(cfg, float(meta.get("started_at") or 0), meta.get("app") or "", meet_codes=[])
            if isinstance(meta.get("calendar"), dict):
                for key in ("override", "pinned"):
                    if key in meta["calendar"]:
                        snap[key] = meta["calendar"][key]
            meta["calendar"] = snap
        decision = decide(snap, duration)
        ev = decision["event"]
        snap["match"] = ({"id": ev["id"], "title": ev["title"], "confidence": decision["confidence"],
                          "confident": True} if ev else
                         {"id": "", "title": "", "confidence": decision["confidence"], "confident": False})
        return decision
    except Exception as e:  # pragma: no cover
        meta.setdefault("calendar", {})["error"] = f"calendar decision failed ({e.__class__.__name__}: {e})"
        return None


# ---------------------------------------------------------------- rendering helpers

def format_when(ev: dict) -> str:
    """"Tue, Sep 30, 2:00–2:30 PM" in local time (spans a day: both dates)."""
    try:
        start, end = _bounds(ev)
    except (ValueError, KeyError, TypeError):
        return ""
    if ev.get("all_day"):
        return f"{start:%a, %b %-d}"
    s, e = start.astimezone(local_tz()), end.astimezone(local_tz())

    def clock(d):
        return f"{d:%-I:%M %p}" if d.minute else f"{d:%-I %p}"

    if s.date() == e.date():
        return f"{s:%a, %b %-d}, {clock(s)}–{clock(e)}"
    return f"{s:%a, %b %-d}, {clock(s)} – {e:%a, %b %-d}, {clock(e)}"


def attendee_names(ev: dict, include_self: bool = False) -> list:
    """Display names for the header and the summarizer: the name on the
    invite, else the address; organizer and declined/tentative marked."""
    org = (ev.get("organizer") or {}).get("email", "")
    out = []
    for a in ev.get("attendees") or []:
        if a.get("self") and not include_self:
            continue
        label = a.get("name") or a.get("email") or ""
        if not label:
            continue
        tags = []
        if org and a.get("email") == org:
            tags.append("organizer")
        if a.get("response") == "declined":
            tags.append("declined")
        elif a.get("response") == "tentative":
            tags.append("tentative")
        if a.get("optional"):
            tags.append("optional")
        out.append(f"{label} ({', '.join(tags)})" if tags else label)
    if not out and ev.get("organizer") and not any(a.get("self") for a in ev.get("attendees") or []):
        o = ev["organizer"]
        label = o.get("name") or o.get("email")
        if label:
            out.append(f"{label} (organizer)")
    return out


def header_lines(decision: dict | None) -> list:
    """Markdown header lines for transcript.md / summary.md."""
    if not decision:
        return []
    ev = decision.get("event")
    if not ev:
        n = sum(1 for r in decision.get("candidates") or [] if not r.get("filtered"))
        if n:
            return [f"**Calendar:** no confident match ({n} candidate{'s' if n != 1 else ''})"]
        return []
    lines = [f"**Meeting:** {ev['title']}"]
    when = format_when(ev)
    if when:
        lines.append(f"**When:** {when}")
    names = attendee_names(ev)
    if names:
        lines.append(f"**Attendees:** {', '.join(names)}")
    return lines


def summary_context(decision: dict | None, cfg: dict) -> str:
    """The calendar's whole contribution to the summarizer's user message
    (process.py builds the model's metadata from this, never from
    header_lines(), whose `**Attendees:**` line is for the local files):
    the meeting title and time always; the invite list only when
    `calendar_names_to_summary`; the description only when
    `calendar_description_to_summary` (off by default, it can carry
    private text)."""
    if not decision or not decision.get("event"):
        return ""
    ev = decision["event"]
    lines = [f"**Meeting:** {ev['title']}"]
    when = format_when(ev)
    if when:
        lines.append(f"**When:** {when}")
    if cfg.get("calendar_names_to_summary", True):
        names = invite_names(ev)
        if names:
            lines.append(f"**People on the invite:** {', '.join(names)}")
    if cfg.get("calendar_description_to_summary", False) and ev.get("description"):
        desc = re.sub(r"\n{3,}", "\n\n", ev["description"].strip())
        lines.append(f"**Event description:**\n{desc}")
    # The last word on anything model-bound: no address, no feed URL, whatever
    # the title or description happened to carry.
    return scrub_for_model("\n".join(lines), cfg)


def invite_names(ev: dict) -> list:
    """The invite list as the summarizer may see it: names only. An invitee
    with no name on the invite is never shown as their address (what
    attendee_names() does for the local header); they are counted instead
    ("and 2 more with no name on the invite")."""
    org = (ev.get("organizer") or {}).get("email", "")
    out = []
    nameless = 0
    for a in ev.get("attendees") or []:
        if a.get("self"):
            continue
        name = (a.get("name") or "").strip()
        if not name or _EMAIL_RE.search(name):
            if a.get("email") or name:
                nameless += 1
            continue
        tags = []
        if org and a.get("email") == org:
            tags.append("organizer")
        if a.get("response") == "declined":
            tags.append("declined")
        elif a.get("response") == "tentative":
            tags.append("tentative")
        if a.get("optional"):
            tags.append("optional")
        out.append(f"{name} ({', '.join(tags)})" if tags else name)
    if not out and not nameless and ev.get("organizer") and not any(a.get("self") for a in ev.get("attendees") or []):
        o = ev["organizer"]
        name = (o.get("name") or "").strip()
        if name and not _EMAIL_RE.search(name):
            out.append(f"{name} (organizer)")
    if nameless:
        out.append(f"and {nameless} more with no name on the invite" if out
                   else f"{nameless} invitee{'s' if nameless != 1 else ''} with no name on the invite")
    return out


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def scrub_addresses(text: str) -> str:
    """Every email address in `text` -> "[address]"."""
    return _EMAIL_RE.sub("[address]", text or "")


def scrub_for_model(text: str, cfg: dict | None = None) -> str:
    """What every model-bound string passes through last: email addresses
    are replaced, and the configured feed address (the credential, in any
    of its forms) is stripped. Only the address in config/env is checked
    here -- never the `_command` form, so this stays free of subprocesses."""
    cfg = cfg or {}
    url = str(cfg.get("calendar_ics_url") or os.environ.get(config.SECRET_ENV.get("calendar_ics_url", ""), "") or "")
    if url:
        # The URL first, before anything alters the text it has to match:
        # a feed address with an unescaped "@" in its path would otherwise
        # be changed by the address scrub and its private token survive.
        # Matched in every spelling: as given, normalized, percent-decoded,
        # and with "@" encoded, each with or without its scheme (_redact).
        forms = []
        for u in (url, _normalize_feed_url(url) or ""):
            if not u:
                continue
            forms += [u, urllib.parse.unquote(u), u.replace("@", "%40"), urllib.parse.unquote(u).replace("@", "%40")]
        text = _redact(text, *forms)
    text = _PRIVATE_SEGMENT_RE.sub("private-<token>", text)  # belt and braces, configured or not
    return scrub_addresses(text)


# A Google-style secret path segment ("/ical/<who>/private-<token>/basic.ics"),
# blanked wherever it appears even when no feed address is configured.
_PRIVATE_SEGMENT_RE = re.compile(r"private-[A-Za-z0-9_\-]{6,}")


def meeting_record(decision: dict | None) -> dict | None:
    """The `meeting` block stored in .transcript.json for later phases
    (speaker naming reads `attendees`). None when there is no confident match."""
    if not decision or not decision.get("event"):
        return None
    ev = decision["event"]
    return {
        "id": ev.get("id", ""),
        "title": ev.get("title", ""),
        "start": ev.get("start", ""),
        "end": ev.get("end", ""),
        "organizer": ev.get("organizer"),
        "attendees": [{"name": a.get("name", ""), "email": a.get("email", ""),
                       "response": a.get("response", ""), "self": bool(a.get("self")),
                       "optional": bool(a.get("optional"))} for a in ev.get("attendees") or []],
        "conference": ev.get("conference"),
        "confidence": decision.get("confidence", 0),
    }


# ---------------------------------------------------------------- CLI

def parse_at(text: str, now: float | None = None) -> float:
    """`--at` for `spitball calendar test`: "HH:MM" (today), "YYYY-MM-DD HH:MM",
    ISO 8601, or epoch seconds -> epoch seconds. Naive times are local."""
    now = time.time() if now is None else now
    t = (text or "").strip()
    if not t or t == "now":
        return now
    if re.match(r"^\d+(\.\d+)?$", t):
        return float(t)
    m = re.match(r"^(\d{1,2}):(\d{2})$", t)
    if m:
        today = datetime.fromtimestamp(now, tz=local_tz())
        return today.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0).timestamp()
    return from_iso(t.replace(" ", "T", 1) if " " in t and "T" not in t else t).timestamp()


def test_report(cfg: dict, at: float, app: str = "", meet_codes=(), refresh: bool = False) -> dict:
    """`spitball calendar test --json`: the source's health plus the match at
    `at`. `ok` is about the SOURCE (reachable and parsable); a missing match
    is still ok. Works whether or not calendar_enabled is on, so a feed can
    be tested before switching it on."""
    kind = source_kind(cfg)
    snap = snapshot(cfg, at, app, meet_codes=list(meet_codes), refresh=refresh)
    decision = decide(snap, None)
    enabled = bool(cfg.get("calendar_enabled"))
    if kind == "off":
        src = str(cfg.get("calendar_source") or "ics")
        msg = ("no feed address set -- paste your calendar's secret iCal address" if src == "ics"
               else "no command set")
        return {"ok": False, "enabled": enabled, "source": "off", "message": msg, "error": msg,
                "events_nearby": 0, "match": None, "confident": False, "confidence": 0,
                "candidates": [], "summary": decision["summary"], "at": at}
    ok = not snap["error"] or bool(snap["cached"])
    if snap["error"] and snap["cached"]:
        message = f"using a cached feed ({snap['error']})"
    elif snap["error"]:
        message = snap["error"]
    else:
        message = ("feed OK" if kind == "ics" else "command OK") + (", cached" if snap["cached"] else "")
    summary = decision["summary"]
    ev = decision["event"]
    # Owner: for the feed, whether we know which address is yours (configured
    # or clearly dominant, never a tie); a command marks `self` itself.
    my_email = str(cfg.get("calendar_my_email") or "").strip().lower() if kind != "ics" else \
        str(snap.get("self_email") or "")
    my_email_known = True if kind != "ics" else bool(snap.get("self_known"))
    return {"ok": ok, "enabled": enabled, "source": kind, "message": f"{message}; {summary}",
            "error": snap["error"], "events_nearby": len(snap["events"]),
            "match": ev, "confident": decision["confident"], "confidence": decision["confidence"],
            "candidates": decision["candidates"], "summary": summary, "at": at,
            "fetched_at": snap["fetched_at"], "cached": snap["cached"],
            "my_email": my_email, "my_email_known": my_email_known,
            "rules_skipped": int(snap.get("rules_skipped") or 0)}


def format_test_report(rep: dict) -> str:
    when = datetime.fromtimestamp(rep.get("at", time.time()), tz=local_tz())
    lines = [f"Calendar: {'on' if rep.get('enabled') else 'off'}; source: {rep.get('source')}",
             f"At: {when:%a, %b %-d %Y, %-I:%M %p}",
             f"Source: {'OK' if rep.get('ok') else 'FAILED'} -- {rep.get('message', '')}"]
    ev = rep.get("match")
    if ev:
        lines.append(f"Match: {ev['title']} ({format_when(ev)}), score {rep.get('confidence')}")
        names = attendee_names(ev)
        if names:
            lines.append(f"Attendees: {', '.join(names)}")
    else:
        lines.append(f"Match: none ({rep.get('summary', '')})")
    if rep.get("source") == "ics" and rep.get("ok"):
        if rep.get("my_email_known"):
            lines.append(f"Your address: {rep.get('my_email') or '(configured)'}")
        else:
            lines.append("Your address: unknown -- no address clearly dominates the feed; set "
                         "calendar_my_email so your own replies are read and speaker naming knows which side is you")
        n = int(rep.get("rules_skipped") or 0)
        if n:
            lines.append(f"Skipped: {n} recurring series with RRULE parts Spitball doesn't expand "
                         "(they never match a call; see CONTRACT.md \"Recurrence\")")
    cands = rep.get("candidates") or []
    if cands:
        lines.append("Candidates:")
        for r in cands:
            tag = f"filtered: {r['filtered']}" if r.get("filtered") else f"score {r['score']}"
            lines.append(f"  - {r['title']} ({format_when(r)}) -- {tag}")
            if r.get("reasons"):
                lines.append(f"      {'; '.join(r['reasons'])}")
    return "\n".join(lines)
