"""spitball/reminders.py: the join-link allowlist (with hostile lookalikes),
reminder eligibility and timing, the fired-reminder log across a restart,
the scheduler with a fake notifier/opener/clock/loader (fires once, join
starts a pinned recording, dismiss does nothing, already recording opens
the link only, no action support falls back to a plain toast), and the
`calendar upcoming` report. Every event is synthetic; nothing here reaches
the network, the real notification server, or xdg-open (the fake notifier
and opener are passed in, and tests/__init__.py's fake binaries are the
backstop)."""
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from spitball import calendar as cal
from spitball import reminders as rem
from tests.testutil import isolated_runtime, make_cfg

DENVER = ZoneInfo("America/Denver")
NOW = datetime(2026, 9, 30, 13, 59, tzinfo=DENVER).timestamp()  # 1:59 PM


def _ev(title="Weekly sync", start=NOW + 60, minutes=30, link="https://meet.google.com/abc-defg-hij",
        response="accepted", **extra):
    s = datetime.fromtimestamp(start, tz=DENVER)
    e = s + timedelta(minutes=minutes)
    conf = cal.conference_from_text(link) if link else None
    if link and conf is None:
        conf = {"kind": "other", "url": link, "code": ""}
    ev = {"id": f"{title}@{cal.to_iso(s)}", "uid": title, "title": title, "start": cal.to_iso(s),
          "end": cal.to_iso(e), "all_day": False, "status": "confirmed", "transparency": "opaque",
          "kind": "default", "my_response": response, "organizer": None,
          "attendees": [{"name": "Alex Demo", "email": "alex@example.com", "response": "accepted",
                         "self": False, "optional": False},
                        {"name": "Me", "email": "owner@example.com", "response": response, "self": True,
                         "optional": False}],
          "conference": conf, "location": "", "description": "", "recurring": False, "recurrence_id": ""}
    ev.update(extra)
    return ev


# ---------------------------------------------------------------- the allowlist

class TestSafeJoinUrl(unittest.TestCase):
    def test_allowlisted_hosts_pass(self):
        for u in ("https://meet.google.com/abc-defg-hij",
                  "https://zoom.us/j/123456789?pwd=abc",
                  "https://us04web.zoom.us/j/123456789",
                  "https://teams.microsoft.com/l/meetup-join/19%3ameeting_x%40thread.v2/0?context=%7b%22Tid%22%3a%22x%22%7d",
                  "https://teams.live.com/meet/9x",
                  "https://company.webex.com/company/j.php?MTID=m1",
                  "https://webex.com/meet/x",
                  "https://ZOOM.US/j/1",
                  "https://zoom.us:443/j/1"):
            self.assertEqual(rem.safe_join_url(u), u, u)

    def test_hostile_lookalikes_are_refused(self):
        for u in ("https://meet.google.com.evil.example/abc-defg-hij",
                  "https://zoom.us@evil.example/j/1",
                  "https://zoom.us:x@evil.example/j/1",
                  "https://evilzoom.us/j/1",
                  "https://zoom.us.evil.example/j/1",
                  "https://evil.example/?u=https://zoom.us/j/1",
                  "https://evil.example/meet.google.com/abc",
                  "https://teams.microsoft.com.evil.example/x",
                  "https://meet.google.com.",
                  "https://meet.google.com:8443/abc-defg-hij",
                  "https://xn--mt-kia.google.com/x",
                  "https://meet.google.com\\@evil.example/x",
                  "https://meet.googlе.com/abc-defg-hij",  # Cyrillic е
                  "https://zoom.us/j/1\nhttps://evil.example",
                  "https://zoom.us/j/1 https://evil.example"):
            self.assertEqual(rem.safe_join_url(u), "", u)

    def test_other_schemes_are_refused(self):
        for u in ("http://meet.google.com/abc-defg-hij",
                  "javascript:alert(1)",
                  "javascript://meet.google.com/%0aalert(1)",
                  "zoommtg://zoom.us/join?confno=1",
                  "zoomus://zoom.us/join?confno=1",
                  "file:///etc/passwd",
                  "data:text/html,hi",
                  "HTTPS:meet.google.com/abc",
                  "//meet.google.com/abc-defg-hij",
                  "meet.google.com/abc-defg-hij"):
            self.assertEqual(rem.safe_join_url(u), "", u)

    def test_garbage_is_refused(self):
        self.assertEqual(rem.safe_join_url(None), "")
        self.assertEqual(rem.safe_join_url(""), "")
        self.assertEqual(rem.safe_join_url(12), "")
        self.assertEqual(rem.safe_join_url("https://zoom.us/" + "a" * 3000), "")
        self.assertEqual(rem.safe_join_url("https://"), "")
        self.assertEqual(rem.safe_join_url("https:///j/1"), "")

    def test_open_link_refuses_anything_off_the_allowlist(self):
        with mock.patch("spitball.reminders.subprocess.Popen") as popen:
            self.assertFalse(rem.open_link("http://meet.google.com/abc-defg-hij"))
            self.assertFalse(rem.open_link("https://evil.example/"))
            popen.assert_not_called()
            self.assertTrue(rem.open_link("https://meet.google.com/abc-defg-hij"))
            argv = popen.call_args[0][0]
            self.assertEqual(argv, ["xdg-open", "https://meet.google.com/abc-defg-hij"])
            self.assertNotIn("shell", popen.call_args.kwargs)
            self.assertTrue(popen.call_args.kwargs.get("start_new_session"))

    def test_open_link_survives_a_missing_opener(self):
        with mock.patch("spitball.reminders.subprocess.Popen", side_effect=OSError("no xdg-open")):
            self.assertFalse(rem.open_link("https://zoom.us/j/1"))


class TestJoinLink(unittest.TestCase):
    def test_conference_link_wins(self):
        self.assertEqual(rem.join_link(_ev()), "https://meet.google.com/abc-defg-hij")

    def test_http_conference_link_is_not_opened(self):
        ev = _ev(link="http://zoom.us/j/1")
        self.assertEqual(ev["conference"]["kind"], "zoom")
        self.assertEqual(rem.join_link(ev), "")

    def test_lookalike_in_the_conference_field_is_refused(self):
        ev = _ev(link=None, conference={"kind": "zoom", "url": "https://evilzoom.us/j/1", "code": "1"})
        self.assertEqual(rem.join_link(ev), "")

    def test_falls_back_to_a_safe_link_in_the_text_fields(self):
        ev = _ev(link=None, location="Room 4 or https://company.webex.com/meet/x.",
                 description="Agenda at https://evil.example/agenda")
        self.assertEqual(rem.join_link(ev), "https://company.webex.com/meet/x")

    def test_no_link_at_all(self):
        self.assertEqual(rem.join_link(_ev(link=None)), "")
        self.assertEqual(rem.join_link(_ev(link=None, description="see https://evil.example/zoom.us/j/1")), "")
        self.assertEqual(rem.join_link({}), "")
        self.assertEqual(rem.join_link(None), "")

    def test_bare_conference_link_is_upgraded_to_the_full_one_from_the_text(self):
        # A calendar_command source handing over the id link, with the
        # invitation link (passcode included) in the location.
        ev = _ev(link=None, conference={"kind": "zoom", "url": "https://zoom.us/j/123", "code": "123"},
                 location="Join: https://zoom.us/j/123?pwd=S3cret.1 (passcode in link)")
        self.assertEqual(rem.join_link(ev), "https://zoom.us/j/123?pwd=S3cret.1")
        # A different link in the text does not replace a good conference link.
        ev = _ev(link=None, conference={"kind": "zoom", "url": "https://zoom.us/j/123", "code": "123"},
                 location="https://zoom.us/j/999?pwd=other")
        self.assertEqual(rem.join_link(ev), "https://zoom.us/j/123")

    def test_link_host(self):
        self.assertEqual(rem.link_host("https://us04web.zoom.us/j/1"), "us04web.zoom.us")
        self.assertEqual(rem.link_host(""), "")


class TestJoinLinkFromIcs(unittest.TestCase):
    """ICS text -> events_from_ics() -> join_link(): the link a reminder
    opens is the invitation link as written, query string included, and
    the matcher's normalized id is untouched."""

    HEAD = "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//test//EN\n"
    TAIL = "END:VCALENDAR\n"

    def _event(self, uid, **props):
        lines = [f"UID:{uid}", "DTSTART:20260930T200100Z", "DTEND:20260930T203000Z", f"SUMMARY:{uid}"]
        lines += [f"{k}:{v}" for k, v in props.items()]
        return "BEGIN:VEVENT\n" + "\n".join(lines) + "\nEND:VEVENT\n"

    def _events(self, body):
        ws = datetime(2026, 9, 30, 0, 0, tzinfo=DENVER)
        return {e["title"]: e for e in cal.events_from_ics(self.HEAD + body + self.TAIL, ws, ws + timedelta(days=1))}

    def test_zoom_passcode_survives_normalization(self):
        evs = self._events(
            self._event("loc", LOCATION="https://us02web.zoom.us/j/123456789?pwd=AbC123.1")
            + self._event("desc", DESCRIPTION="Join Zoom Meeting\\nhttps://zoom.us/j/987?pwd=x9Y.2\\nMeeting ID: 987")
            + self._event("prose", LOCATION="Zoom (https://zoom.us/j/555?pwd=p.3)."))
        self.assertEqual(evs["loc"]["conference"], {"kind": "zoom", "url": "https://us02web.zoom.us/j/123456789?pwd=AbC123.1",
                                                    "code": "123456789"})
        self.assertEqual(rem.join_link(evs["loc"]), "https://us02web.zoom.us/j/123456789?pwd=AbC123.1")
        self.assertEqual(rem.join_link(evs["desc"]), "https://zoom.us/j/987?pwd=x9Y.2")
        self.assertEqual(evs["desc"]["conference"]["code"], "987")
        self.assertEqual(rem.join_link(evs["prose"]), "https://zoom.us/j/555?pwd=p.3")

    def test_teams_and_webex_keep_their_query_context(self):
        teams = ("https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc%40thread.v2/0"
                 "?context=%7b%22Tid%22%3a%22t1%22%2c%22Oid%22%3a%22o1%22%7d")
        webex = "https://acme.webex.com/acme/j.php?MTID=m0123456789abcdef"
        evs = self._events(self._event("teams", **{"X-MICROSOFT-SKYPETEAMSMEETINGURL": teams})
                           + self._event("webex", LOCATION=webex))
        self.assertEqual(rem.join_link(evs["teams"]), teams)
        self.assertEqual(evs["teams"]["conference"]["kind"], "teams")
        self.assertEqual(rem.join_link(evs["webex"]), webex)

    def test_meet_link_with_a_query_keeps_its_code(self):
        evs = self._events(self._event("meet", **{"X-GOOGLE-CONFERENCE": "https://meet.google.com/abc-defg-hij?hs=224"}))
        self.assertEqual(evs["meet"]["conference"]["code"], "abc-defg-hij")
        self.assertEqual(rem.join_link(evs["meet"]), "https://meet.google.com/abc-defg-hij?hs=224")

    def test_hostile_links_with_a_matching_id_are_never_opened(self):
        evs = self._events(
            self._event("lookalike", LOCATION="https://evilzoom.us/j/123456789?pwd=abc")
            + self._event("userinfo", LOCATION="https://zoom.us@evil.example/j/123456789?pwd=abc")
            + self._event("http", LOCATION="http://zoom.us/j/123456789?pwd=abc")
            + self._event("suffix", DESCRIPTION="https://zoom.us.evil.example/j/123456789?pwd=abc")
            + self._event("fragment", LOCATION="https://teams.microsoft.com.evil.example/l/meetup-join/x#y"))
        for title, ev in evs.items():
            self.assertEqual(rem.join_link(ev), "", title)
            self.assertEqual(rem.skip_reason(ev), "no meeting link", title)
        # The parser still records the lookalike as a zoom-shaped link (the
        # matcher's host heuristics are not a security boundary); the
        # allowlist is what refuses it.
        self.assertEqual(evs["lookalike"]["conference"]["code"], "123456789")

    def test_fixture_zoom_event_opens_with_its_passcode(self):
        ws = datetime(2026, 9, 30, 0, 0, tzinfo=DENVER)
        evs = cal.events_from_ics((cal.Path(__file__).resolve().parent / "fixtures" / "ics" / "basic.ics").read_text(),
                                  ws, ws + timedelta(days=1), "owner@example.com")
        zoom = [e for e in evs if (e.get("conference") or {}).get("kind") == "zoom"]
        self.assertTrue(zoom)
        self.assertEqual(rem.join_link(zoom[0]), "https://us02web.zoom.us/j/123456789?pwd=abc")


# ---------------------------------------------------------------- eligibility and timing

class TestPlan(unittest.TestCase):
    def test_fires_at_lead_time_not_before(self):
        ev = _ev(start=NOW + 60)
        self.assertEqual(rem.due([ev], NOW - 1, 60, set()), [])
        hits = rem.due([ev], NOW, 60, set())
        self.assertEqual([h["event"]["title"] for h in hits], ["Weekly sync"])
        self.assertEqual(hits[0]["host"], "meet.google.com")
        self.assertEqual(hits[0]["fire_at"], NOW)

    def test_lead_time_is_configurable(self):
        ev = _ev(start=NOW + 300)
        self.assertEqual(rem.due([ev], NOW, 60, set()), [])
        self.assertEqual(len(rem.due([ev], NOW, 300, set())), 1)

    def test_late_start_within_grace_still_fires(self):
        ev = _ev(start=NOW - 90)
        self.assertEqual(len(rem.due([ev], NOW, 60, set())), 1)

    def test_too_late_never_fires(self):
        ev = _ev(start=NOW - rem.LATE_GRACE_S - 1)
        self.assertEqual(rem.due([ev], NOW, 60, set()), [])
        upcoming, _ = rem.plan([ev], NOW, 60)
        self.assertEqual(upcoming, [])

    def test_already_reminded_is_skipped(self):
        ev = _ev()
        key = rem.occurrence_key(ev)
        self.assertEqual(rem.due([ev], NOW, 60, {key}), [])
        upcoming, _ = rem.plan([ev], NOW, 60, {key})
        self.assertTrue(upcoming[0]["reminded"])

    def test_rescheduled_occurrence_is_a_new_key(self):
        a = _ev(start=NOW + 60)
        b = dict(a, start=cal.to_iso(datetime.fromtimestamp(NOW + 3600, tz=DENVER)))
        self.assertNotEqual(rem.occurrence_key(a), rem.occurrence_key(b))

    def test_hard_filters_and_missing_link_skip(self):
        cases = {
            "declined": _ev(response="declined", my_response="declined"),
            "all-day": _ev(all_day=True),
            "cancelled": _ev(status="cancelled"),
            "marked free": _ev(transparency="transparent"),
            "focus time": _ev(kind="focus"),
            "out of office": _ev(kind="out_of_office"),
            "no meeting link": _ev(link=None),
        }
        for reason, ev in cases.items():
            self.assertEqual(rem.skip_reason(ev), reason)
        upcoming, skipped = rem.plan(list(cases.values()) + [_ev(title="Good")], NOW, 60)
        self.assertEqual([u["event"]["title"] for u in upcoming], ["Good"])
        self.assertEqual(skipped, len(cases))

    def test_tentative_and_unanswered_still_remind(self):
        for resp in ("tentative", "needs_action", ""):
            self.assertEqual(rem.skip_reason(_ev(response=resp, my_response=resp)), "")

    def test_sorted_by_start(self):
        late, early = _ev(title="B", start=NOW + 7200), _ev(title="A", start=NOW + 600)
        upcoming, _ = rem.plan([late, early], NOW, 60)
        self.assertEqual([u["event"]["title"] for u in upcoming], ["A", "B"])

    def test_bad_start_is_skipped_not_raised(self):
        upcoming, skipped = rem.plan([_ev(start=NOW + 60) | {"start": "soon"}], NOW, 60)
        self.assertEqual((upcoming, skipped), ([], 1))

    def test_lead_seconds_clamps(self):
        self.assertEqual(rem.lead_seconds({"calendar_remind_before_s": 60}), 60)
        self.assertEqual(rem.lead_seconds({"calendar_remind_before_s": -5}), 0)
        self.assertEqual(rem.lead_seconds({"calendar_remind_before_s": 99999}), 3600)
        self.assertEqual(rem.lead_seconds({"calendar_remind_before_s": "x"}), 60)
        self.assertEqual(rem.lead_seconds({}), 60)

    def test_enabled_needs_calendar_on_and_a_source(self):
        base = {"calendar_enabled": True, "calendar_reminders": True, "calendar_source": "command",
                "calendar_command": "true"}
        self.assertTrue(rem.enabled(base))
        self.assertFalse(rem.enabled(dict(base, calendar_enabled=False)))
        self.assertFalse(rem.enabled(dict(base, calendar_reminders=False)))
        self.assertFalse(rem.enabled(dict(base, calendar_command="")))

    def test_when_label(self):
        self.assertEqual(rem.when_label(NOW + 60, NOW), "in 1 min")
        self.assertEqual(rem.when_label(NOW + 61, NOW), "in 2 min")
        self.assertEqual(rem.when_label(NOW + 300, NOW), "in 5 min")
        self.assertEqual(rem.when_label(NOW + 10, NOW), "now")
        self.assertEqual(rem.when_label(NOW - 30, NOW), "now")
        self.assertEqual(rem.when_label(NOW - 100, NOW), "started 2 min ago")

    def test_notification_text_names_spitball_and_the_host(self):
        entry = rem.plan([_ev()], NOW, 60)[0][0]
        summary, body = rem.notification_text(entry, NOW, True)
        self.assertEqual(summary, "Spitball reminder: Weekly sync in 1 min")
        self.assertIn("2:00 PM–2:30 PM · meet.google.com", body)
        self.assertIn("Click to join and record.", body)
        _, plain = rem.notification_text(entry, NOW, False)
        self.assertIn("record button in the bar", plain)
        self.assertNotIn("abc-defg-hij", summary + body + plain)  # the link itself is never shown


# ---------------------------------------------------------------- the fired log

class TestReminderLog(unittest.TestCase):
    def test_persists_across_instances_and_prunes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "calendar" / "reminded.json"
            log = rem.ReminderLog(path)
            self.assertNotIn("a@1", log)
            log.mark("a@1", NOW)
            self.assertIn("a@1", log)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            again = rem.ReminderLog(path)
            self.assertIn("a@1", again)
            again.mark("b@2", NOW + rem.LOG_KEEP_S + 1)  # marks, and prunes the old one
            self.assertEqual(rem.ReminderLog(path).keys(), {"b@2"})

    def test_corrupt_file_is_an_empty_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reminded.json"
            path.write_text("{not json")
            self.assertEqual(rem.ReminderLog(path).keys(), set())
            path.write_text(json.dumps({"ok": 1.0, "bad": "x", "list": [1]}))
            self.assertEqual(rem.ReminderLog(path).keys(), {"ok"})

    def test_default_path_is_under_state_dir(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(Path(tmp)) as config:
            self.assertEqual(rem.ReminderLog().path, config.STATE_DIR / "calendar" / "reminded.json")


# ---------------------------------------------------------------- desktop notifier

class TestDesktopNotify(unittest.TestCase):
    def test_command_shape_and_answer(self):
        calls = []

        def run(cmd, **kw):
            calls.append((cmd, kw))
            return mock.Mock(returncode=0, stdout="default\n", stderr="")
        r = rem.desktop_notify("S", "B", [("default", "Join & record"), ("dismiss", "Dismiss")], 700, 180000, run=run)
        self.assertEqual(r["action"], "default")
        cmd, kw = calls[0]
        self.assertEqual(cmd[:3], ["notify-send", "--app-name", "Spitball"])
        self.assertIn("-A", cmd)
        self.assertEqual(cmd[cmd.index("-A") + 1], "default=Join & record")
        self.assertEqual(cmd[-2:], ["S", "B"])
        self.assertEqual(kw["timeout"], 700)
        self.assertNotIn("shell", kw)

    def test_plain_toast_has_no_actions_and_a_short_timeout(self):
        calls = []

        def run(cmd, **kw):
            calls.append((cmd, kw))
            return mock.Mock(returncode=0, stdout="", stderr="")
        r = rem.desktop_notify("S", "B", [], 700, 1000, run=run)
        self.assertEqual(r["action"], "")
        self.assertNotIn("-A", calls[0][0])
        self.assertEqual(calls[0][1]["timeout"], rem.NOTIFY_TIMEOUT_S)

    def test_timeout_and_missing_binary_are_answers(self):
        import subprocess
        r = rem.desktop_notify("S", "B", [("default", "x")], 1, 1000,
                               run=mock.Mock(side_effect=subprocess.TimeoutExpired("notify-send", 1)))
        self.assertEqual((r["action"], r["timed_out"]), ("", True))
        r = rem.desktop_notify("S", "B", [], 1, 1000, run=mock.Mock(side_effect=OSError("nope")))
        self.assertEqual(r["action"], "")

    def test_server_capabilities_parse(self):
        run = mock.Mock(return_value=mock.Mock(returncode=0, stdout="(['actions', 'body', 'persistence'],)\n"))
        self.assertEqual(rem.server_capabilities(run=run), ["actions", "body", "persistence"])
        self.assertIsNone(rem.server_capabilities(run=mock.Mock(side_effect=OSError())))
        self.assertIsNone(rem.server_capabilities(run=mock.Mock(return_value=mock.Mock(returncode=1, stdout=""))))


# ---------------------------------------------------------------- the scheduler

class FakeDesk:
    """A fake notifier + opener + join sink. `answer` is what the user clicks."""

    def __init__(self, answer="default", caps=("actions", "body"), stderr=""):
        self.answer, self.caps, self.stderr = answer, list(caps) if caps is not None else None, stderr
        self.toasts, self.opened, self.joined = [], [], []
        self.gate = threading.Event()
        self.gate.set()

    def notify(self, summary, body, actions, wait_s, expire_ms):
        self.toasts.append({"summary": summary, "body": body, "actions": actions, "wait_s": wait_s,
                            "expire_ms": expire_ms})
        self.gate.wait(10)
        return {"action": self.answer if actions else "", "rc": 0, "stderr": self.stderr, "timed_out": False}

    def opener(self, link):
        self.opened.append(link)
        return True

    def join(self, event, link):
        self.joined.append((event["id"], link))
        return {"ok": True}

    def capabilities(self):
        return self.caps


def _wait_threads(name="reminder"):
    for t in threading.enumerate():
        if t.name == name:
            t.join(10)


class TestScheduler(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp = Path(self.tmpdir.name)
        self.cfg = make_cfg(self.tmp, calendar_enabled=True, calendar_source="command",
                            calendar_command="true", calendar_reminders=True, calendar_remind_before_s=60)
        self.now = NOW
        self.recording = False
        self.events = [_ev()]
        self.desk = FakeDesk()

    def make(self, desk=None):
        desk = desk or self.desk
        return rem.Scheduler(cfg=lambda: self.cfg, recording=lambda: self.recording, join=desk.join,
                             notify=desk.notify, opener=desk.opener,
                             loader=lambda cfg, now: (list(self.events), {"error": "", "source": "command"}),
                             clock=lambda: self.now, capabilities=desk.capabilities,
                             log=rem.ReminderLog(self.tmp / "reminded.json"))

    def test_fires_once_and_join_opens_then_records_pinned(self):
        s = self.make()
        s.refresh_now()
        s.tick()
        _wait_threads()
        self.assertEqual(len(self.desk.toasts), 1)
        t = self.desk.toasts[0]
        self.assertEqual(t["summary"], "Spitball reminder: Weekly sync in 1 min")
        self.assertEqual([a[0] for a in t["actions"]], ["default", "dismiss"])
        self.assertEqual(t["actions"][0][1], "Join & record")
        self.assertEqual(self.desk.opened, ["https://meet.google.com/abc-defg-hij"])
        self.assertEqual(self.desk.joined, [(self.events[0]["id"], "https://meet.google.com/abc-defg-hij")])
        # Same tick again, and later ticks: nothing more.
        s.tick()
        self.now += 30
        s.tick()
        _wait_threads()
        self.assertEqual(len(self.desk.toasts), 1)
        self.assertEqual(s.fired, 1)

    def test_dismiss_does_nothing(self):
        desk = FakeDesk(answer="dismiss")
        s = self.make(desk)
        s.refresh_now()
        s.tick()
        _wait_threads()
        self.assertEqual(len(desk.toasts), 1)
        self.assertEqual((desk.opened, desk.joined), ([], []))

    def test_closed_without_an_answer_does_nothing(self):
        desk = FakeDesk(answer="")
        s = self.make(desk)
        s.refresh_now()
        s.tick()
        _wait_threads()
        self.assertEqual((desk.opened, desk.joined), ([], []))

    def test_already_recording_skips_the_reminder_entirely(self):
        self.recording = True
        s = self.make()
        s.refresh_now()
        s.tick()
        _wait_threads()
        self.assertEqual(self.desk.toasts, [])
        self.assertIn(rem.occurrence_key(self.events[0]), s.log)  # and it won't fire later either
        self.recording = False
        s.tick()
        _wait_threads()
        self.assertEqual(self.desk.toasts, [])

    def test_recording_started_while_the_toast_was_up_only_opens_the_link(self):
        self.desk.gate.clear()
        s = self.make()
        s.refresh_now()
        s.tick()
        self.recording = True   # the user hit Record in the bar meanwhile
        self.desk.gate.set()
        _wait_threads()
        self.assertEqual(self.desk.opened, ["https://meet.google.com/abc-defg-hij"])
        self.assertEqual(self.desk.joined, [])

    def test_restart_does_not_refire(self):
        s = self.make()
        s.refresh_now()
        s.tick()
        _wait_threads()
        self.assertEqual(len(self.desk.toasts), 1)
        fresh = self.make()  # a new daemon, same state dir
        fresh.refresh_now()
        fresh.tick()
        _wait_threads()
        self.assertEqual(len(self.desk.toasts), 1)

    def test_resume_after_suspend_skips_stale_meetings(self):
        self.events = [_ev(title="Missed", start=NOW - 600), _ev(title="Soon", start=NOW + 60)]
        s = self.make()
        s.refresh_now()
        s.tick()
        _wait_threads()
        self.assertEqual([t["summary"] for t in self.desk.toasts], ["Spitball reminder: Soon in 1 min"])

    def test_disabled_never_loads_or_fires(self):
        self.cfg["calendar_reminders"] = False
        s = self.make()
        s.tick()
        _wait_threads()
        self.assertEqual(self.desk.toasts, [])
        self.assertEqual(s.events, [])
        self.cfg["calendar_reminders"] = True
        self.cfg["calendar_enabled"] = False
        s.tick()
        self.assertEqual(s.events, [])

    def test_reload_applies_live(self):
        s = self.make()
        self.cfg["calendar_remind_before_s"] = 30
        s.refresh_now()
        s.tick()   # 60 s out: not yet with a 30 s lead
        _wait_threads()
        self.assertEqual(self.desk.toasts, [])
        self.now += 30
        s.tick()
        _wait_threads()
        self.assertEqual(len(self.desk.toasts), 1)
        self.assertEqual(self.desk.toasts[0]["summary"], "Spitball reminder: Weekly sync now")

    def test_no_action_support_falls_back_to_a_plain_toast(self):
        desk = FakeDesk(caps=["body"])
        s = self.make(desk)
        s.refresh_now()
        s.tick()
        _wait_threads()
        self.assertEqual(desk.toasts[0]["actions"], [])
        self.assertIn("record button in the bar", desk.toasts[0]["body"])
        self.assertEqual((desk.opened, desk.joined), ([], []))

    def test_unknown_capabilities_try_actions_then_learn_from_stderr(self):
        desk = FakeDesk(caps=None, answer="", stderr="Actions are not supported by this notifications server.")
        self.events = [_ev(title="One", start=NOW + 60), _ev(title="Two", start=NOW + 3600)]
        s = self.make(desk)
        s.refresh_now()
        s.tick()
        _wait_threads()
        self.assertEqual(len(desk.toasts[0]["actions"]), 2)
        self.assertFalse(s.actions_ok)
        self.now += 3540
        s.tick()
        _wait_threads()
        self.assertEqual(desk.toasts[1]["actions"], [])

    def test_refresh_runs_off_the_tick_and_keeps_old_events_on_failure(self):
        s = self.make()
        s.refresh_now()
        self.assertEqual(len(s.events), 1)

        def boom(cfg, now):
            raise RuntimeError("feed down")
        s._loader = boom
        s.loaded_at = 0
        s.tick()
        _wait_threads("reminder-feed")
        self.assertEqual(len(s.events), 1)
        self.assertIn("feed down", s.error)
        _wait_threads()
        self.assertEqual(len(self.desk.toasts), 1)   # the due reminder still fired from the old list

    def test_tick_survives_a_raising_notifier(self):
        def bad(*a, **k):
            raise RuntimeError("toast exploded")
        s = self.make()
        s._notify = bad
        s.refresh_now()
        s.tick()
        _wait_threads()
        self.assertIn("toast exploded", s.error)
        self.assertEqual(s.pending, set())

    def test_snapshot_lists_upcoming_with_fired_flags(self):
        self.events = [_ev(title="A", start=NOW + 60), _ev(title="B", start=NOW + 1800), _ev(title="C", link=None)]
        s = self.make()
        s.refresh_now()
        s.tick()
        _wait_threads()
        snap = s.snapshot()
        self.assertEqual([(u["event"]["title"], u["reminded"]) for u in snap["upcoming"]], [("A", True), ("B", False)])
        self.assertEqual(snap["skipped"], 1)
        self.assertTrue(snap["enabled"])

    def test_join_refuses_a_link_that_turned_unsafe(self):
        s = self.make()
        s._do_join({"event": self.events[0], "link": "http://meet.google.com/abc", "key": "k"})
        self.assertEqual((self.desk.opened, self.desk.joined), ([], []))


# ---------------------------------------------------------------- `calendar upcoming`

class TestUpcomingReport(unittest.TestCase):
    def _cfg(self, tmp, events):
        script = tmp / "events.py"
        script.write_text("import json,sys\nprint(json.dumps(" + repr(events) + "))\n")
        return make_cfg(tmp, calendar_enabled=True, calendar_source="command",
                        calendar_command=f"python3 {script}")

    def test_report_and_text(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(Path(tmp)):
            tmp = Path(tmp)
            events = [{"title": "Weekly sync", "start": cal.to_iso(datetime.fromtimestamp(NOW + 60, tz=DENVER)),
                       "end": cal.to_iso(datetime.fromtimestamp(NOW + 1860, tz=DENVER)),
                       "location": "https://zoom.us/j/555", "attendees": ["Alex <alex@example.com>"]},
                      {"title": "Lunch", "start": cal.to_iso(datetime.fromtimestamp(NOW + 3600, tz=DENVER)),
                       "end": cal.to_iso(datetime.fromtimestamp(NOW + 7200, tz=DENVER))},
                      {"title": "Old", "start": cal.to_iso(datetime.fromtimestamp(NOW - 7200, tz=DENVER)),
                       "end": cal.to_iso(datetime.fromtimestamp(NOW - 3600, tz=DENVER)),
                       "location": "https://zoom.us/j/1"}]
            rep = rem.upcoming_report(self._cfg(tmp, events), now=NOW, hours=6)
            self.assertTrue(rep["ok"])
            self.assertTrue(rep["active"])
            self.assertEqual([u["title"] for u in rep["upcoming"]], ["Weekly sync"])
            self.assertEqual(rep["upcoming"][0]["host"], "zoom.us")
            self.assertEqual(rep["upcoming"][0]["link"], "https://zoom.us/j/555")
            self.assertEqual(rep["upcoming"][0]["fire_at"], NOW)
            self.assertEqual(rep["upcoming"][0]["attendees"], 1)
            self.assertFalse(rep["upcoming"][0]["reminded"])
            self.assertEqual(rep["skipped"], 1)   # Lunch (no link); Old is simply past, not "skipped"
            text = rem.format_upcoming_report(rep)
            self.assertIn("Reminders: on; 60 s before; source: command", text)
            self.assertIn("Weekly sync  (zoom.us) -- due now", text)
            self.assertIn("Skipped: 1 event ", text)
            # Once the daemon has fired it, the report says so.
            rem.ReminderLog().mark(f"{rep['upcoming'][0]['id']}@{rep['upcoming'][0]['start']}", NOW)
            rep2 = rem.upcoming_report(self._cfg(tmp, events), now=NOW, hours=6)
            self.assertTrue(rep2["upcoming"][0]["reminded"])
            self.assertIn("-- fired", rem.format_upcoming_report(rep2))

    def test_off_states(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(Path(tmp)):
            tmp = Path(tmp)
            cfg = make_cfg(tmp)
            rep = rem.upcoming_report(cfg, now=NOW)
            self.assertFalse(rep["ok"])
            self.assertEqual(rep["source"], "off")
            self.assertIn("no calendar source", rep["error"])
            cfg = self._cfg(tmp, [])
            cfg["calendar_reminders"] = False
            rep = rem.upcoming_report(cfg, now=NOW)
            self.assertTrue(rep["ok"])
            self.assertFalse(rep["active"])
            text = rem.format_upcoming_report(rep)
            self.assertIn("off (calendar_reminders is false)", text)
            self.assertIn("No meetings with a video link", text)

    def test_broken_command_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(Path(tmp)):
            cfg = make_cfg(Path(tmp), calendar_enabled=True, calendar_source="command", calendar_command="exit 3")
            rep = rem.upcoming_report(cfg, now=NOW)
            self.assertFalse(rep["ok"])
            self.assertIn("exited 3", rep["error"])
            self.assertIn("FAILED", rem.format_upcoming_report(rep))


if __name__ == "__main__":
    unittest.main()
