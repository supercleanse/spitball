"""spitball/calendar.py: the ICS parser (unfolding, quoted parameters,
escapes, TZID/UTC/all-day, nested VALARM), the bounded recurrence expander
(WEEKLY/DAILY/MONTHLY/YEARLY, INTERVAL, COUNT, UNTIL, BYDAY incl. ordinals,
EXDATE, RECURRENCE-ID overrides and cancellations, DST), the sources (ICS
over a local http.server with the on-disk cache; calendar_command), the
matcher (window, hard filters, scoring, Meet code, threshold + margin,
overlap tie-break), and the rendering helpers process.py uses.

Every fixture under tests/fixtures/ics/ is synthetic. Nothing here reaches
the network or a real calendar; the ICS "server" is a local http.server on
127.0.0.1, and STATE_DIR is a temp dir (isolated_runtime), so the cache never
lands in ~/.local/state/spitball.
"""
import http.server
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from spitball import calendar as cal
from tests.testutil import FIXTURES, isolated_runtime, make_cfg

ICS = FIXTURES / "ics"
DENVER = ZoneInfo("America/Denver")
# 2:03 PM Denver on Wed 2026-09-30 -- three minutes into "Weekly sync".
T_SYNC = datetime(2026, 9, 30, 14, 3, tzinfo=DENVER).timestamp()


def _window(center: float, days: float = 1.0):
    t = datetime.fromtimestamp(center, tz=timezone.utc)
    return t - timedelta(days=days), t + timedelta(days=days)


def _load(name: str, at: float = T_SYNC, days: float = 1.0, my_email: str = "owner@example.com"):
    ws, we = _window(at, days)
    return cal.events_from_ics((ICS / name).read_text(), ws, we, my_email)


def _by_title(events: list) -> dict:
    out = {}
    for e in events:
        out.setdefault(e["title"], []).append(e)
    return out


def _ev(title="Ev", start=T_SYNC - 180, minutes=30, attendees=1, response="accepted", conf=None,
        **extra):
    """A normalized event built by hand for matcher tests."""
    s = datetime.fromtimestamp(start, tz=DENVER)
    e = s + timedelta(minutes=minutes)
    people = [{"name": f"Person {i}", "email": f"p{i}@example.com", "response": "accepted",
               "self": False, "optional": False} for i in range(attendees)]
    people.append({"name": "Me", "email": "owner@example.com", "response": response, "self": True,
                   "optional": False})
    ev = {"id": f"{title}@{cal.to_iso(s)}", "uid": title, "title": title, "start": cal.to_iso(s),
          "end": cal.to_iso(e), "all_day": False, "status": "confirmed", "transparency": "opaque",
          "kind": "default", "my_response": response, "organizer": None, "attendees": people,
          "conference": conf, "location": "", "description": "", "recurring": False,
          "recurrence_id": ""}
    ev.update(extra)
    return ev


# ---------------------------------------------------------------- parsing

class TestLineLevel(unittest.TestCase):
    def test_unfold_handles_crlf_lf_and_tab_continuations(self):
        text = "A:one\r\n two\nB:x\n\tyz\nC:last"
        self.assertEqual(cal.unfold(text), ["A:onetwo", "B:xyz", "C:last"])

    def test_parse_property_with_quoted_params(self):
        name, params, value = cal.parse_property(
            'ATTENDEE;CN="Doe, Jane";X-RESPONSE-COMMENT="a; b: c";PARTSTAT=ACCEPTED:mailto:j@x.io')
        self.assertEqual(name, "ATTENDEE")
        self.assertEqual(params["CN"], ["Doe, Jane"])
        self.assertEqual(params["X-RESPONSE-COMMENT"], ["a; b: c"])
        self.assertEqual(params["PARTSTAT"], ["ACCEPTED"])
        self.assertEqual(value, "mailto:j@x.io")

    def test_parse_property_multi_valued_param_and_lowercase_name(self):
        name, params, value = cal.parse_property("exdate;TZID=America/Denver;VALUE=DATE-TIME:20260101T000000,20260102T000000")
        self.assertEqual(name, "EXDATE")
        self.assertEqual(params["TZID"], ["America/Denver"])
        self.assertEqual(value, "20260101T000000,20260102T000000")

    def test_unescape(self):
        self.assertEqual(cal.unescape(r"a\nb\, c\; d\\e"), "a\nb, c; d\\e")


class TestParseBasic(unittest.TestCase):
    def setUp(self):
        self.events = _load("basic.ics")
        self.by = _by_title(self.events)

    def test_every_event_present_once_and_valarm_skipped(self):
        titles = sorted(self.by)
        self.assertEqual(titles, ["1:1 with Sam", "Conference day", "Design review", "Focus time",
                                  "Old planning", "Out of office", "Team lunch", "Teams chat",
                                  "Vendor call", "Weekly sync"])
        for t in titles:
            self.assertEqual(len(self.by[t]), 1, t)
        self.assertNotIn("This is an event reminder", json.dumps(self.events))

    def test_tzid_utc_and_all_day_forms(self):
        sync = self.by["Weekly sync"][0]
        self.assertEqual(sync["start"], "2026-09-30T14:00:00-06:00")
        self.assertEqual(sync["end"], "2026-09-30T14:30:00-06:00")
        self.assertFalse(sync["all_day"])
        review = self.by["Design review"][0]
        self.assertEqual(review["start"], "2026-09-30T20:00:00+00:00")
        conf = self.by["Conference day"][0]
        self.assertTrue(conf["all_day"])
        self.assertEqual(conf["start"], "2026-09-30")
        self.assertEqual(conf["end"], "2026-10-01")

    def test_windows_tzid_maps_to_iana(self):
        teams = self.by["Teams chat"][0]
        self.assertEqual(teams["start"], "2026-09-30T18:00:00-04:00")

    def test_attendees_names_responses_self_and_resources(self):
        sync = self.by["Weekly sync"][0]
        people = {a["email"]: a for a in sync["attendees"]}
        self.assertEqual(set(people), {"alex@example.com", "owner@example.com", "bo@example.com"})
        self.assertEqual(people["alex@example.com"]["name"], "Alex Demo")
        self.assertEqual(people["alex@example.com"]["response"], "accepted")
        self.assertTrue(people["owner@example.com"]["self"])
        self.assertEqual(people["owner@example.com"]["name"], "")  # CN equal to the address is dropped
        self.assertEqual(people["bo@example.com"]["response"], "declined")
        self.assertTrue(people["bo@example.com"]["optional"])
        self.assertEqual(sync["organizer"], {"name": "Alex Demo", "email": "alex@example.com"})
        self.assertEqual(sync["my_response"], "accepted")
        self.assertEqual(self.by["Design review"][0]["my_response"], "needs_action")
        self.assertEqual(self.by["1:1 with Sam"][0]["my_response"], "declined")

    def test_self_detected_when_not_configured(self):
        events = _load("basic.ics", my_email="")
        sync = _by_title(events)["Weekly sync"][0]
        self.assertEqual(sync["my_response"], "accepted")
        self.assertTrue(any(a["self"] for a in sync["attendees"]))

    def test_conference_links(self):
        self.assertEqual(self.by["Weekly sync"][0]["conference"],
                         {"kind": "meet", "url": "https://meet.google.com/abc-defg-hij", "code": "abc-defg-hij"})
        self.assertEqual(self.by["Design review"][0]["conference"]["code"], "xyz-abcd-efg")
        zoom = self.by["Vendor call"][0]["conference"]
        self.assertEqual(zoom["kind"], "zoom")
        self.assertEqual(zoom["code"], "123456789")
        self.assertEqual(self.by["Teams chat"][0]["conference"]["kind"], "teams")
        self.assertIsNone(self.by["Focus time"][0]["conference"])

    def test_description_unfolded_and_unescaped(self):
        desc = self.by["Weekly sync"][0]["description"]
        self.assertIn("1. Numbers, then plans\n2. Q&A", desc)
        self.assertIn("Join with Google Meet: https://meet.google.com/abc-defg-hij", desc)

    def test_status_transparency_kind(self):
        self.assertEqual(self.by["Old planning"][0]["status"], "cancelled")
        self.assertEqual(self.by["Team lunch"][0]["transparency"], "transparent")
        self.assertEqual(self.by["Focus time"][0]["kind"], "focus")
        self.assertEqual(self.by["Out of office"][0]["kind"], "out_of_office")
        self.assertEqual(self.by["Weekly sync"][0]["kind"], "default")

    def test_event_kind_heuristics(self):
        self.assertEqual(cal.event_kind("Focus"), "focus")
        self.assertEqual(cal.event_kind("OOO - beach"), "out_of_office")
        self.assertEqual(cal.event_kind("WFH"), "working_location")
        self.assertEqual(cal.event_kind("Pat's birthday"), "birthday")
        self.assertEqual(cal.event_kind("Focus group with customers"), "default")
        self.assertEqual(cal.event_kind("Office hours"), "default")

    def test_ids_are_stable_and_recurring_flag_false(self):
        sync = self.by["Weekly sync"][0]
        self.assertEqual(sync["id"], "weekly-sync@example.com")
        self.assertFalse(sync["recurring"])

    def test_window_excludes_far_events(self):
        events = _load("basic.ics", at=T_SYNC + 10 * 86400)
        self.assertEqual(events, [])


# ---------------------------------------------------------------- recurrence

class TestRecurrence(unittest.TestCase):
    def setUp(self):
        # Mon 2026-09-28 .. Fri 2026-10-02 (Denver), a week around the fixture's dates.
        ws = datetime(2026, 9, 28, 0, 0, tzinfo=DENVER)
        we = datetime(2026, 10, 3, 0, 0, tzinfo=DENVER)
        self.events = cal.events_from_ics((ICS / "recurrence.ics").read_text(), ws, we, "owner@example.com")
        self.by = _by_title(self.events)

    def _starts(self, title):
        return sorted(e["start"] for e in self.by.get(title, []))

    def test_weekly_byday_with_exdate_override_and_cancelled_instance(self):
        # MO/WE/FR that week: 9/28 (canceled override), 9/30 (EXDATE), 10/2 (moved to 10:00).
        self.assertEqual(self._starts("Standup"), [])
        self.assertEqual(self._starts("Standup (moved)"), ["2026-10-02T10:00:00-06:00"])
        moved = self.by["Standup (moved)"][0]
        self.assertTrue(moved["recurring"])
        self.assertEqual(moved["id"], "standup@example.com/2026-10-02T09:00:00-06:00")
        self.assertEqual(moved["recurrence_id"], "2026-10-02T09:00:00-06:00")

    def test_weekly_instances_before_the_exceptions(self):
        ws = datetime(2026, 9, 21, 0, 0, tzinfo=DENVER)
        we = datetime(2026, 9, 26, 0, 0, tzinfo=DENVER)
        events = cal.events_from_ics((ICS / "recurrence.ics").read_text(), ws, we, "owner@example.com")
        starts = sorted(e["start"] for e in events if e["title"] == "Standup")
        self.assertEqual(starts, ["2026-09-21T09:00:00-06:00", "2026-09-23T09:00:00-06:00",
                                  "2026-09-25T09:00:00-06:00"])
        self.assertTrue(all(e["recurring"] for e in events if e["title"] == "Standup"))
        # The series' attendees/conference carry to every instance.
        first = [e for e in events if e["title"] == "Standup"][0]
        self.assertEqual(first["conference"]["code"], "sta-ndup-abc")
        self.assertEqual(len(first["attendees"]), 2)

    def test_daily_count(self):
        self.assertEqual(self._starts("Check-in"), [
            "2026-09-28T08:00:00-06:00", "2026-09-29T08:00:00-06:00", "2026-09-30T08:00:00-06:00",
            "2026-10-01T08:00:00-06:00", "2026-10-02T08:00:00-06:00"])
        ws = datetime(2026, 10, 3, 0, 0, tzinfo=DENVER)
        later = cal.events_from_ics((ICS / "recurrence.ics").read_text(), ws, ws + timedelta(days=7),
                                    "owner@example.com")
        self.assertNotIn("Check-in", _by_title(later))

    def test_monthly_last_wednesday(self):
        self.assertEqual(self._starts("Board meeting"), ["2026-09-30T15:00:00-06:00"])
        ws = datetime(2026, 10, 26, 0, 0, tzinfo=DENVER)
        oct_ = cal.events_from_ics((ICS / "recurrence.ics").read_text(), ws, ws + timedelta(days=7),
                                   "owner@example.com")
        self.assertEqual(sorted(e["start"] for e in oct_ if e["title"] == "Board meeting"),
                         ["2026-10-28T15:00:00-06:00"])

    def test_weekly_interval_two(self):
        self.assertEqual(self._starts("Biweekly retro"), ["2026-09-30T11:00:00-06:00"])
        ws = datetime(2026, 10, 5, 0, 0, tzinfo=DENVER)
        nxt = cal.events_from_ics((ICS / "recurrence.ics").read_text(), ws, ws + timedelta(days=7),
                                  "owner@example.com")
        self.assertNotIn("Biweekly retro", _by_title(nxt))  # 10/7 is the off week
        ws = datetime(2026, 10, 12, 0, 0, tzinfo=DENVER)
        nxt2 = cal.events_from_ics((ICS / "recurrence.ics").read_text(), ws, ws + timedelta(days=7),
                                   "owner@example.com")
        self.assertEqual(sorted(e["start"] for e in nxt2 if e["title"] == "Biweekly retro"),
                         ["2026-10-14T11:00:00-06:00"])

    def test_yearly_all_day(self):
        self.assertEqual(self._starts("Anniversary"), ["2026-09-30"])
        self.assertTrue(self.by["Anniversary"][0]["all_day"])

    def test_dst_keeps_wall_clock_in_the_events_zone(self):
        # 16:00 Berlin in March (CET) stays 16:00 Berlin in September (CEST).
        self.assertEqual(self._starts("Europe sync"), ["2026-09-30T16:00:00+02:00"])

    def test_override_moved_into_window_from_outside(self):
        # The 10/8 instance was pulled forward to 9/30 15:00; the regular 10/1 instance stays.
        self.assertEqual(self._starts("Planning (pulled forward)"), ["2026-09-30T15:00:00-06:00"])
        self.assertEqual(self._starts("Planning"), ["2026-10-01T10:00:00-06:00"])

    def test_forever_daily_rule_is_fast_forwarded_not_walked(self):
        t0 = time.perf_counter()
        self.assertEqual(len(self.by["Forever daily"]), 5)
        self.assertLess(time.perf_counter() - t0, 0.05)
        self.assertEqual(self._starts("Forever daily")[0], "2026-09-28T07:00:00-06:00")

    def test_until_in_the_past_yields_nothing(self):
        self.assertNotIn("Ended series", self.by)

    def test_parse_rrule(self):
        r = cal.parse_rrule("FREQ=MONTHLY;INTERVAL=2;BYDAY=2TU,-1FR;COUNT=3;WKST=SU;BYMONTHDAY=1,-1")
        self.assertEqual(r["FREQ"], "MONTHLY")
        self.assertEqual(r["INTERVAL"], 2)
        self.assertEqual(r["BYDAY"], [(2, 1), (-1, 4)])
        self.assertEqual(r["COUNT"], 3)
        self.assertEqual(r["WKST"], 6)
        self.assertEqual(r["BYMONTHDAY"], [1, -1])

    def test_expand_rrule_count_counts_exdated_occurrences(self):
        start = datetime(2026, 9, 1, 9, 0, tzinfo=DENVER)
        rule = cal.parse_rrule("FREQ=DAILY;COUNT=3")
        ex = {cal._key(start + timedelta(days=1))}
        ws, we = start - timedelta(days=1), start + timedelta(days=30)
        got = cal.expand_rrule(start, rule, ex, ws, we, timedelta(minutes=30))
        self.assertEqual(got, [start, start + timedelta(days=2)])

    def test_expand_rrule_iteration_cap(self):
        start = datetime(2000, 1, 1, 9, 0, tzinfo=DENVER)
        rule = cal.parse_rrule("FREQ=WEEKLY;COUNT=999999")
        ws = datetime(2026, 9, 28, tzinfo=DENVER)
        t0 = time.perf_counter()
        got = cal.expand_rrule(start, rule, set(), ws, ws + timedelta(days=7), timedelta(minutes=30))
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(got), 1)


# ---------------------------------------------------------------- calendar_command

class TestExternalEvents(unittest.TestCase):
    def test_normalize_external_lenient_shapes(self):
        ev = cal.normalize_external({
            "title": "Coffee chat", "start": "2026-09-30T14:00:00-06:00", "end": "2026-09-30T14:30:00-06:00",
            "attendees": ["Pat Demo <pat@example.com>", "Just A Name",
                          {"name": "Robin", "email": "ROBIN@example.com", "responseStatus": "accepted", "self": True}],
            "organizer": "Pat Demo", "hangoutLink": "https://meet.google.com/aaa-bbbb-ccc", "eventType": "default"})
        self.assertEqual(ev["title"], "Coffee chat")
        self.assertEqual(ev["start"], "2026-09-30T14:00:00-06:00")
        self.assertEqual([a["email"] for a in ev["attendees"]], ["pat@example.com", "", "robin@example.com"])
        self.assertEqual(ev["attendees"][1]["name"], "Just A Name")
        self.assertEqual(ev["my_response"], "accepted")
        self.assertEqual(ev["conference"]["code"], "aaa-bbbb-ccc")
        self.assertEqual(ev["organizer"], {"name": "Pat Demo", "email": ""})
        self.assertEqual(ev["kind"], "default")

    def test_normalize_external_epoch_and_bare_date(self):
        ev = cal.normalize_external({"title": "T", "start": 1790000000, "end": 1790003600})
        self.assertEqual(cal.from_iso(ev["start"]).timestamp(), 1790000000)
        ev = cal.normalize_external({"title": "Day", "start": "2026-09-30", "end": "2026-10-01"})
        self.assertTrue(ev["all_day"])
        self.assertEqual(ev["start"], "2026-09-30")

    def test_normalize_external_rejects_junk(self):
        self.assertIsNone(cal.normalize_external({"start": "2026-09-30T14:00:00"}))
        self.assertIsNone(cal.normalize_external({"title": "x", "start": "not a date"}))
        self.assertIsNone(cal.normalize_external("nope"))

    def test_run_command_passes_window_and_filters_to_it(self):
        ws, we = _window(T_SYNC)
        script = ("python3 -c \"import json,os; print(json.dumps([{'title': 'In', 'start': '2026-09-30T14:00:00-06:00', "
                  "'end': '2026-09-30T14:30:00-06:00'}, {'title': 'Far', 'start': '2027-01-01T14:00:00-06:00', "
                  "'end': '2027-01-01T15:00:00-06:00'}, {'title': 'Env', 'start': os.environ['SPITBALL_WINDOW_START'], "
                  "'end': os.environ['SPITBALL_WINDOW_END']}]))\"")
        cfg = {"calendar_command": script}
        events, info = cal.run_command(cfg, ws, we)
        self.assertEqual(info["error"], "")
        self.assertEqual(sorted(e["title"] for e in events), ["Env", "In"])

    def test_run_command_accepts_events_object(self):
        ws, we = _window(T_SYNC)
        cfg = {"calendar_command": "echo '{\"events\": [{\"title\": \"A\", \"start\": \"2026-09-30T14:00:00-06:00\"}]}'"}
        events, info = cal.run_command(cfg, ws, we)
        self.assertEqual([e["title"] for e in events], ["A"])
        self.assertEqual(cal.from_iso(events[0]["end"]) - cal.from_iso(events[0]["start"]), timedelta(hours=1))

    def test_run_command_failures_are_reported_not_raised(self):
        ws, we = _window(T_SYNC)
        _, info = cal.run_command({"calendar_command": "exit 3"}, ws, we)
        self.assertIn("exited 3", info["error"])
        _, info = cal.run_command({"calendar_command": "echo nope"}, ws, we)
        self.assertIn("isn't JSON", info["error"])
        _, info = cal.run_command({"calendar_command": "echo '\"a string\"'"}, ws, we)
        self.assertIn("JSON array", info["error"])
        _, info = cal.run_command({"calendar_command": ""}, ws, we)
        self.assertIn("no calendar_command", info["error"])


# ---------------------------------------------------------------- ICS source + cache

class _IcsHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a, **kw):
        pass

    def do_GET(self):
        self.server.requests.append({"path": self.path, "headers": dict(self.headers)})
        status, body, headers = self.server.behavior(self)
        data = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class TestIcsSource(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp = Path(self.tmpdir.name)
        self._rt = isolated_runtime(self.tmp)
        self.config = self._rt.__enter__()
        self.addCleanup(self._rt.__exit__, None, None, None)
        self.text = (ICS / "basic.ics").read_text()
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _IcsHandler)
        self.srv.requests = []
        self.srv.behavior = lambda h: (200, self.text, {"Content-Type": "text/calendar", "ETag": '"v1"'})
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/calendar/ical/private-SECRET/basic.ics"

    def _stop(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(timeout=5)

    def _cfg(self, **over):
        cfg = make_cfg(self.tmp, calendar_enabled=True, calendar_source="ics", calendar_ics_url=self.url,
                       calendar_cache_ttl_s=900)
        cfg.update(over)
        return cfg

    def test_fetch_then_cache_within_ttl(self):
        cfg = self._cfg()
        text, info = cal.fetch_ics(cfg, now=1000.0)
        self.assertIn("BEGIN:VCALENDAR", text)
        self.assertFalse(info["cached"])
        self.assertEqual(info["error"], "")
        self.assertEqual(len(self.srv.requests), 1)
        self.assertIn("Spitball", self.srv.requests[0]["headers"].get("User-Agent", ""))
        feed = self.config.STATE_DIR / "calendar" / "feed.ics"
        self.assertTrue(feed.exists())
        self.assertEqual(stat.S_IMODE(feed.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.config.STATE_DIR / "calendar").stat().st_mode), 0o700)
        # The cache metadata never holds the URL itself.
        meta = (self.config.STATE_DIR / "calendar" / "feed.json").read_text()
        self.assertNotIn("SECRET", meta)
        self.assertNotIn(self.url, meta)
        text2, info2 = cal.fetch_ics(cfg, now=1500.0)
        self.assertTrue(info2["cached"])
        self.assertEqual(len(self.srv.requests), 1)  # no second hit
        self.assertEqual(text2, text)

    def test_stale_cache_revalidates_with_etag_and_304(self):
        cfg = self._cfg()
        cal.fetch_ics(cfg, now=1000.0)
        self.srv.behavior = lambda h: (304, b"", {}) if h.headers.get("If-None-Match") == '"v1"' else (200, "BEGIN:VCALENDAR\nEND:VCALENDAR", {})
        text, info = cal.fetch_ics(cfg, now=1000.0 + 901)
        self.assertEqual(len(self.srv.requests), 2)
        self.assertEqual(self.srv.requests[1]["headers"].get("If-None-Match"), '"v1"')
        self.assertTrue(info["cached"])
        self.assertIn("weekly-sync@example.com", text)  # the cached body, not the tiny 200 fallback
        meta = json.loads((self.config.STATE_DIR / "calendar" / "feed.json").read_text())
        self.assertEqual(meta["fetched_at"], 1901.0)  # refreshed by the 304

    def test_refresh_forces_a_fetch(self):
        cfg = self._cfg()
        cal.fetch_ics(cfg, now=1000.0)
        cal.fetch_ics(cfg, now=1001.0, refresh=True)
        self.assertEqual(len(self.srv.requests), 2)

    def test_server_error_falls_back_to_stale_cache_with_message(self):
        cfg = self._cfg()
        cal.fetch_ics(cfg, now=1000.0)
        self.srv.behavior = lambda h: (404, "gone", {})
        text, info = cal.fetch_ics(cfg, now=5000.0)
        self.assertIn("BEGIN:VCALENDAR", text)
        self.assertTrue(info["cached"])
        self.assertIn("HTTP 404", info["error"])
        self.assertIn("reset", info["error"])
        self.assertNotIn("SECRET", info["error"])

    def test_no_cache_and_server_error_means_no_feed(self):
        cfg = self._cfg()
        self.srv.behavior = lambda h: (500, "boom", {})
        text, info = cal.fetch_ics(cfg, now=1000.0)
        self.assertIsNone(text)
        self.assertIn("HTTP 500", info["error"])

    def test_unreachable_host_is_a_message_not_an_exception(self):
        cfg = self._cfg(calendar_ics_url="http://127.0.0.1:1/private-SECRET/x.ics")
        text, info = cal.fetch_ics(cfg, now=1000.0)
        self.assertIsNone(text)
        self.assertIn("unreachable", info["error"])
        self.assertNotIn("SECRET", info["error"])

    def test_non_ics_body_rejected(self):
        cfg = self._cfg()
        self.srv.behavior = lambda h: (200, "<html>sign in</html>", {})
        text, info = cal.fetch_ics(cfg, now=1000.0)
        self.assertIsNone(text)
        self.assertIn("iCalendar", info["error"])

    def test_changed_url_invalidates_cache(self):
        cfg = self._cfg()
        cal.fetch_ics(cfg, now=1000.0)
        cfg2 = self._cfg(calendar_ics_url=self.url + "?other")
        cal.fetch_ics(cfg2, now=1001.0)
        self.assertEqual(len(self.srv.requests), 2)

    def test_url_from_command_and_webcal_scheme(self):
        cfg = self._cfg(calendar_ics_url="", calendar_ics_url_command=f"echo '{self.url}'")
        text, info = cal.fetch_ics(cfg, now=1000.0)
        self.assertIn("BEGIN:VCALENDAR", text)
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            raise OSError("stop here")

        cfg = self._cfg(calendar_ics_url="webcal://example.test/feed.ics")
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            cal.fetch_ics(cfg, now=1000.0)
        self.assertEqual(captured["url"], "https://example.test/feed.ics")

    def test_load_events_ics_end_to_end(self):
        cfg = self._cfg()
        ws, we = _window(T_SYNC)
        events, info = cal.load_events(cfg, ws, we)
        self.assertEqual(info["source"], "ics")
        self.assertIn("Weekly sync", {e["title"] for e in events})

    def test_source_kind(self):
        self.assertEqual(cal.source_kind(self._cfg()), "ics")
        self.assertEqual(cal.source_kind(self._cfg(calendar_ics_url="")), "off")
        self.assertEqual(cal.source_kind(self._cfg(calendar_ics_url="", calendar_ics_url_command="echo x")), "ics")
        self.assertEqual(cal.source_kind(self._cfg(calendar_source="command", calendar_command="echo []")), "command")
        self.assertEqual(cal.source_kind(self._cfg(calendar_source="command", calendar_command="")), "off")
        self.assertEqual(cal.load_events(self._cfg(calendar_ics_url=""), *_window(T_SYNC))[1]["source"], "off")


# ---------------------------------------------------------------- matcher

class TestMatcher(unittest.TestCase):
    def test_meet_code_is_the_exact_key(self):
        a = _ev("Weekly sync", conf={"kind": "meet", "url": "", "code": "abc-defg-hij"})
        b = _ev("Design review", attendees=2, conf={"kind": "meet", "url": "", "code": "xyz-abcd-efg"})
        r = cal.match([a, b], T_SYNC, None, ["abc-defg-hij"], "Chrome")
        self.assertTrue(r["confident"])
        self.assertEqual(r["event"]["title"], "Weekly sync")
        self.assertGreaterEqual(r["confidence"], 100)
        rows = {row["title"]: row for row in r["candidates"]}
        self.assertIn("Meet code in a window title", rows["Weekly sync"]["reasons"])
        self.assertIn("a different Meet code is open", rows["Design review"]["reasons"])
        self.assertLessEqual(rows["Design review"]["score"], 0)
        self.assertNotIn("Meet link, browser on the mic", rows["Design review"]["reasons"])

    def test_two_overlapping_events_without_a_key_is_no_match(self):
        a = _ev("A", attendees=2)
        b = _ev("B", attendees=2)
        r = cal.match([a, b], T_SYNC)
        self.assertFalse(r["confident"])
        self.assertIsNone(r["event"])
        self.assertIn("no confident match (2 candidates", r["summary"])

    def test_margin_required_over_runner_up(self):
        a = _ev("A", attendees=3)  # 20 + 30 + 10 = 60
        b = _ev("B", attendees=2)  # 20 + 20 + 10 = 50 -> margin 10 < 15
        r = cal.match([a, b], T_SYNC)
        self.assertFalse(r["confident"])
        c = _ev("C", attendees=0, response="needs_action")  # 20 + 0 + 5 = 25
        r = cal.match([a, c], T_SYNC)
        self.assertTrue(r["confident"])
        self.assertEqual(r["event"]["title"], "A")

    def test_solo_block_never_matches(self):
        r = cal.match([_ev("Deep work", attendees=0, response="")], T_SYNC)
        self.assertFalse(r["confident"])
        self.assertEqual(r["confidence"], 20)

    def test_one_to_one_on_time_matches(self):
        r = cal.match([_ev("1:1", attendees=1)], T_SYNC)
        self.assertTrue(r["confident"])
        self.assertEqual(r["confidence"], 40)

    def test_hard_filters(self):
        base = T_SYNC - 180
        events = [
            _ev("All day", all_day=True, start=base - 3600 * 5, minutes=60 * 24),
            _ev("Cancelled", status="cancelled"),
            _ev("Free", transparency="transparent"),
            _ev("Focus", kind="focus"),
            _ev("OOO", kind="out_of_office"),
            _ev("Declined", response="declined"),
            _ev("Real", attendees=2),
        ]
        r = cal.match(events, T_SYNC)
        rows = {row["title"]: row["filtered"] for row in r["candidates"]}
        self.assertEqual(rows, {"All day": "all-day", "Cancelled": "cancelled", "Free": "marked free",
                                "Focus": "focus time", "OOO": "out of office", "Declined": "declined",
                                "Real": ""})
        self.assertEqual(r["event"]["title"], "Real")

    def test_only_filtered_events_summary(self):
        r = cal.match([_ev("Focus", kind="focus")], T_SYNC)
        self.assertIn("all filtered out", r["summary"])
        self.assertEqual(cal.match([], T_SYNC)["summary"], "no events at that time")

    def test_window_before_and_after(self):
        early = _ev("Early", start=T_SYNC + 14 * 60)         # starts 14 min after the recording began
        too_early = _ev("Too early", start=T_SYNC + 16 * 60)
        just_over = _ev("Just over", start=T_SYNC - 39 * 60, minutes=30)   # ended 9 min ago
        long_over = _ev("Long over", start=T_SYNC - 45 * 60, minutes=30)   # ended 15 min ago
        titles = {row["title"] for row in cal.match([early, too_early, just_over, long_over], T_SYNC)["candidates"]}
        self.assertEqual(titles, {"Early", "Just over"})

    def test_late_start_penalty_capped(self):
        late = _ev("Late", start=T_SYNC - 60 * 60, minutes=120, attendees=3)  # joined an hour in
        s, reasons = cal.score_event(late, T_SYNC, None, [], "")
        self.assertEqual(s, 20 + 30 + 10 - 30)
        self.assertTrue(any("60 min from the event start" in x for x in reasons))

    def test_overlap_breaks_back_to_back_once_duration_is_known(self):
        first = _ev("First", start=T_SYNC - 28 * 60, minutes=30, attendees=1)
        second = _ev("Second", start=T_SYNC + 2 * 60, minutes=30, attendees=2)
        r_start = cal.match([first, second], T_SYNC, None)
        self.assertFalse(r_start["confident"])  # 20+10+10-30=10 vs 0+20+10=30: below threshold
        r_end = cal.match([first, second], T_SYNC, 30 * 60)
        self.assertTrue(r_end["confident"])
        self.assertEqual(r_end["event"]["title"], "Second")
        rows = {row["title"]: row for row in r_end["candidates"]}
        self.assertTrue(any("% of the recording inside the event" in x for x in rows["Second"]["reasons"]))

    def test_conference_host_agrees_or_conflicts_with_app(self):
        zoom = _ev("Zoom", conf={"kind": "zoom", "url": "", "code": "1"})
        meet = _ev("Meet", conf={"kind": "meet", "url": "", "code": "abc-defg-hij"})
        none = _ev("Huddle")
        self.assertEqual(cal.score_event(zoom, T_SYNC, None, [], "Zoom")[0], 30 + 20 + 10 + 10)
        self.assertEqual(cal.score_event(meet, T_SYNC, None, [], "Chrome")[0], 30 + 20 + 10 + 10)
        self.assertEqual(cal.score_event(meet, T_SYNC, None, [], "Zoom")[0], -30 + 20 + 10 + 10)
        self.assertEqual(cal.score_event(none, T_SYNC, None, [], "Slack")[0], 30 + 20 + 10 + 10)
        self.assertEqual(cal.score_event(none, T_SYNC, None, [], "")[0], 20 + 10 + 10)
        self.assertEqual(cal.score_event(zoom, T_SYNC, None, [], "Firefox")[0], 15 + 20 + 10 + 10)

    def test_declined_others_do_not_count_as_attendees(self):
        ev = _ev("X", attendees=0)
        ev["attendees"].append({"name": "Gone", "email": "g@example.com", "response": "declined",
                                "self": False, "optional": False})
        s, reasons = cal.score_event(ev, T_SYNC, None, [], "")
        self.assertEqual(s, 30)
        self.assertFalse(any("attendee" in x for x in reasons))

    def test_match_on_fixture_at_two_pm(self):
        events = _load("basic.ics")
        r = cal.match(events, T_SYNC, None, ["abc-defg-hij"], "Chrome")
        self.assertTrue(r["confident"])
        self.assertEqual(r["event"]["title"], "Weekly sync")
        r2 = cal.match(events, T_SYNC, None, [], "Chrome")
        # Two live Meet events overlap 2:03 PM; without a code it's a toss-up.
        self.assertFalse(r2["confident"])
        self.assertIn("no confident match", r2["summary"])
        r3 = cal.match(events, datetime(2026, 9, 30, 16, 2, tzinfo=DENVER).timestamp(), None, [], "Zoom")
        self.assertTrue(r3["confident"])
        self.assertEqual(r3["event"]["title"], "Vendor call")

    def test_decide_honors_override(self):
        events = [_ev("A", attendees=2), _ev("B", attendees=2)]
        snap = {"events": events, "started_at": T_SYNC, "meet_codes": [], "app": ""}
        self.assertFalse(cal.decide(snap)["confident"])
        snap["override"] = {"event": events[1]["id"]}
        d = cal.decide(snap)
        self.assertTrue(d["confident"])
        self.assertEqual(d["event"]["title"], "B")
        self.assertIn("set by hand", d["summary"])
        snap["override"] = {"event": None}
        d = cal.decide(snap)
        self.assertIsNone(d["event"])
        self.assertIn("set by hand", d["summary"])
        snap["override"] = {"event": "nope"}
        self.assertIn("not among the candidates", cal.decide(snap)["summary"])
        self.assertEqual(cal.decide(None)["summary"], "no events at that time")


# ---------------------------------------------------------------- snapshot / for_call / helpers

class TestSnapshotAndHelpers(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp = Path(self.tmpdir.name)
        self._rt = isolated_runtime(self.tmp)
        self._rt.__enter__()
        self.addCleanup(self._rt.__exit__, None, None, None)
        self.cmd = ("python3 -c \"import json; print(json.dumps([{'title': 'Weekly sync', "
                    "'start': '2026-09-30T14:00:00-06:00', 'end': '2026-09-30T14:30:00-06:00', "
                    "'attendees': [{'name': 'Alex Demo', 'email': 'alex@example.com', 'response': 'accepted'}, "
                    "{'name': 'Bo', 'email': 'bo@example.com', 'response': 'declined'}, "
                    "{'name': 'Me', 'email': 'owner@example.com', 'response': 'accepted', 'self': True}], "
                    "'organizer': {'name': 'Alex Demo', 'email': 'alex@example.com'}, "
                    "'description': 'Agenda\\\\n\\\\n\\\\n1. Things', "
                    "'conference': 'https://meet.google.com/abc-defg-hij'}, "
                    "{'title': 'Lunch', 'start': '2026-09-30T12:00:00-06:00', 'end': '2026-09-30T13:00:00-06:00'}]))\"")
        self.cfg = make_cfg(self.tmp, calendar_enabled=True, calendar_source="command", calendar_command=self.cmd)

    def test_snapshot_keeps_only_candidates_and_never_raises(self):
        snap = cal.snapshot(self.cfg, T_SYNC, "Chrome", meet_codes=["abc-defg-hij"])
        self.assertEqual(snap["source"], "command")
        self.assertEqual([e["title"] for e in snap["events"]], ["Weekly sync"])  # Lunch is outside the window
        self.assertEqual(snap["meet_codes"], ["abc-defg-hij"])
        self.assertEqual(snap["app"], "Chrome")
        self.assertEqual(snap["error"], "")
        bad = cal.snapshot(dict(self.cfg, calendar_command="exit 1"), T_SYNC, "", meet_codes=[])
        self.assertEqual(bad["events"], [])
        self.assertIn("exited 1", bad["error"])

    def test_snapshot_reads_window_titles_when_not_given(self):
        with mock.patch("spitball.calendar.window_meet_codes", return_value=["zzz-zzzz-zzz"]) as w:
            snap = cal.snapshot(self.cfg, T_SYNC, "Chrome")
        w.assert_called_once()
        self.assertEqual(snap["meet_codes"], ["zzz-zzzz-zzz"])

    def test_window_meet_codes_from_hyprctl_titles(self):
        clients = [{"class": "chromium", "title": "Meet – abc-defg-hij - Chromium"},
                   {"class": "zoom", "title": "Zoom Meeting"},
                   {"class": "chromium", "title": "Meet – abc-defg-hij - Chromium"},
                   {"class": "foot", "title": "~"}]
        run = mock.Mock(return_value=mock.Mock(stdout=json.dumps(clients)))
        self.assertEqual(cal.window_meet_codes(run), ["abc-defg-hij"])
        self.assertEqual(run.call_args.args[0][:2], ["hyprctl", "clients"])
        self.assertEqual(cal.window_meet_codes(mock.Mock(side_effect=OSError("no hyprctl"))), [])
        self.assertEqual(cal.window_meet_codes(mock.Mock(return_value=mock.Mock(stdout="garbage"))), [])

    def test_for_call_uses_stored_snapshot_else_looks_up(self):
        meta = {"app": "Chrome", "started_at": T_SYNC, "duration": 1500}
        d = cal.for_call(meta, self.cfg, 1500)
        self.assertTrue(d["confident"])
        self.assertEqual(meta["calendar"]["match"]["title"], "Weekly sync")
        self.assertTrue(meta["calendar"]["match"]["confident"])
        # A stored snapshot is used as-is, even if the command is gone now.
        d2 = cal.for_call(meta, dict(self.cfg, calendar_command="exit 1"), 1500)
        self.assertEqual(d2["event"]["title"], "Weekly sync")
        # Off, with no snapshot: None and nothing stored.
        meta3 = {"app": "", "started_at": T_SYNC, "duration": 1}
        self.assertIsNone(cal.for_call(meta3, dict(self.cfg, calendar_enabled=False), 1))
        self.assertNotIn("calendar", meta3)
        # A stored override survives a fresh lookup.
        meta4 = {"app": "", "started_at": T_SYNC, "duration": 1, "calendar": {"override": {"event": None}}}
        d4 = cal.for_call(meta4, self.cfg, 1)
        self.assertIsNone(d4["event"])
        self.assertEqual(meta4["calendar"]["override"], {"event": None})

    def test_rendering_helpers(self):
        d = cal.for_call({"app": "Chrome", "started_at": T_SYNC, "duration": 1500}, self.cfg, 1500)
        ev = d["event"]
        self.assertEqual(cal.attendee_names(ev), ["Alex Demo (organizer)", "Bo (declined)"])
        with mock.patch("spitball.calendar.local_tz", return_value=DENVER):
            self.assertEqual(cal.format_when(ev), "Wed, Sep 30, 2 PM–2:30 PM")
            lines = cal.header_lines(d)
        self.assertEqual(lines, ["**Meeting:** Weekly sync", "**When:** Wed, Sep 30, 2 PM–2:30 PM",
                                 "**Attendees:** Alex Demo (organizer), Bo (declined)"])
        ctx = cal.summary_context(d, self.cfg)
        self.assertIn("**People on the invite:** Alex Demo (organizer), Bo (declined)", ctx)
        self.assertNotIn("Agenda", ctx)
        ctx2 = cal.summary_context(d, dict(self.cfg, calendar_description_to_summary=True, calendar_names_to_summary=False))
        self.assertIn("**Event description:**\nAgenda\n\n1. Things", ctx2)
        self.assertNotIn("People on the invite", ctx2)
        rec = cal.meeting_record(d)
        self.assertEqual(rec["title"], "Weekly sync")
        self.assertEqual([a["name"] for a in rec["attendees"]], ["Alex Demo", "Bo", "Me"])
        self.assertTrue(rec["attendees"][2]["self"])
        self.assertEqual(rec["conference"]["code"], "abc-defg-hij")
        self.assertIsNone(cal.meeting_record(None))
        self.assertEqual(cal.header_lines(None), [])

    def test_header_for_weak_match_counts_candidates(self):
        r = cal.match([_ev("A", attendees=2), _ev("B", attendees=2)], T_SYNC)
        self.assertEqual(cal.header_lines(r), ["**Calendar:** no confident match (2 candidates)"])
        self.assertEqual(cal.header_lines(cal.match([_ev("F", kind="focus")], T_SYNC)), [])
        self.assertEqual(cal.summary_context(r, self.cfg), "")

    def test_parse_at(self):
        now = T_SYNC
        self.assertEqual(cal.parse_at("now", now), now)
        self.assertEqual(cal.parse_at("", now), now)
        self.assertEqual(cal.parse_at("1790000000", now), 1790000000.0)
        with mock.patch("spitball.calendar.local_tz", return_value=DENVER):
            self.assertEqual(cal.parse_at("14:30", now), datetime(2026, 9, 30, 14, 30, tzinfo=DENVER).timestamp())
            self.assertEqual(cal.parse_at("2026-09-30 14:30", now), datetime(2026, 9, 30, 14, 30, tzinfo=DENVER).timestamp())
        self.assertEqual(cal.parse_at("2026-09-30T20:30:00Z", now), datetime(2026, 9, 30, 20, 30, tzinfo=timezone.utc).timestamp())
        with self.assertRaises(ValueError):
            cal.parse_at("yesterday-ish", now)

    def test_test_report_shapes(self):
        rep = cal.test_report(self.cfg, T_SYNC, app="Chrome", meet_codes=["abc-defg-hij"])
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["source"], "command")
        self.assertEqual(rep["match"]["title"], "Weekly sync")
        self.assertTrue(rep["confident"])
        self.assertIn("matched “Weekly sync”", rep["message"])
        text = cal.format_test_report(rep)
        self.assertIn("Match: Weekly sync", text)
        self.assertIn("Candidates:", text)
        off = cal.test_report(dict(self.cfg, calendar_command=""), T_SYNC)
        self.assertFalse(off["ok"])
        self.assertEqual(off["source"], "off")
        self.assertIn("no command set", off["message"])
        off_ics = cal.test_report(dict(self.cfg, calendar_source="ics"), T_SYNC)
        self.assertIn("secret iCal address", off_ics["message"])
        broken = cal.test_report(dict(self.cfg, calendar_command="exit 2"), T_SYNC)
        self.assertFalse(broken["ok"])
        self.assertIn("exited 2", broken["message"])
        self.assertIsNone(broken["match"])


if __name__ == "__main__":
    unittest.main()
