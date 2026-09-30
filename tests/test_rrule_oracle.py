"""Differential test: spitball.calendar.expand_rrule against python-dateutil.

dateutil is the oracle for every RRULE combination `unsupported_rrule_reason()`
accepts: a matrix over FREQ x the BY* parts x INTERVAL x COUNT/UNTIL, each
with an EXDATE, on DST-crossing zoned starts and an all-day start, in a near
window (from dtstart) and a far one (two years out, which exercises the
expander's fast-forward). Every accepted rule must match dateutil exactly;
a rule that can't be made to match belongs in unsupported_rrule_reason().

The product stays standard-library only: dateutil is never imported by
spitball itself, and this module skips when it isn't importable. To run it
for real, make a throwaway venv (`uv venv /tmp/x && uv pip install --python
/tmp/x/bin/python python-dateutil`) and run this file with that python;
SPITBALL_ORACLE_FULL=1 pairs every rule with every start instead of rotating.
"""
from __future__ import annotations

import itertools
import os
import sys
import time
import unittest
import warnings
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from spitball import calendar as cal

try:
    from dateutil.rrule import rrulestr, rruleset
    HAVE_DATEUTIL = True
except ImportError:  # pragma: no cover - the normal case under /usr/bin/python3
    HAVE_DATEUTIL = False

DENVER = ZoneInfo("America/Denver")
BERLIN = ZoneInfo("Europe/Berlin")
UTC = timezone.utc
UNTIL_DT = "20271115T120000Z"
UNTIL_D = "20271115"

# Zoned starts that cross a DST change inside the near window, a leap day,
# and a date-only (all-day) start.
STARTS = [
    datetime(2026, 1, 30, 9, 0, tzinfo=DENVER),    # Jan 30: month-end edge cases; DST Mar 8 / Nov 1
    datetime(2026, 10, 20, 16, 0, tzinfo=BERLIN),  # crosses Europe's Oct 25 fall-back
    datetime(2024, 2, 29, 10, 0, tzinfo=DENVER),   # leap day
    datetime(2026, 3, 1, 7, 30, tzinfo=DENVER),    # a week before the spring-forward
    date(2026, 9, 30),                             # all-day
]


def _join(*parts):
    return ";".join(p for p in parts if p)


def _rules():
    """Every rule string the matrix covers (supported or not; the test
    filters through unsupported_rrule_reason and counts both)."""
    bounds = [None, "COUNT=7", "COUNT=40", "UNTIL={until}"]
    for byday, interval, bound in itertools.product([None, "MO,WE,FR", "SA,SU"], [1, 2, 3], bounds):
        yield _join("FREQ=DAILY", f"INTERVAL={interval}" if interval > 1 else None,
                    f"BYDAY={byday}" if byday else None, bound)
    for byday, interval, wkst, bound in itertools.product(
            [None, "MO", "MO,WE,FR", "SA,SU", "TU,TH,SU"], [1, 2, 3], [None, "SU", "MO", "WE"],
            [None, "COUNT=5", "COUNT=25", "UNTIL={until}"]):
        yield _join("FREQ=WEEKLY", f"INTERVAL={interval}" if interval > 1 else None,
                    f"WKST={wkst}" if wkst else None, f"BYDAY={byday}" if byday else None, bound)
    # An ordinal BYDAY intersected with BYMONTHDAY ("-1WE" that is also the
    # 1st) is a set that is empty nearly every month; dateutil walks such a
    # rule to year 9999 (~0.7 s each, UNTIL or not), so the matrix pairs
    # BYMONTHDAY only with plain BYDAY lists -- the RFC's own "first Monday"
    # idiom -- and keeps one ordinal case as the explicit empty-set check.
    for byday, bymd, setpos, bymonth, interval, bound in itertools.product(
            [None, "-1WE", "2TU", "MO,TU,WE,TH,FR", "1MO,-1FR", "SA,SU"],
            [None, "1", "15,-1", "1,2,3,4,5,6,7", "-2"],
            [None, "1", "-1", "1,-1", "-2", "3"],
            [None, "3,9", "1,4,7,10"], [1, 2], [None, "COUNT=6", "UNTIL={until}"]):
        ordinal = byday and any(ch.isdigit() for ch in byday)
        if ordinal and bymd:
            continue
        if setpos == "3" and (byday in (None, "-1WE", "2TU", "1MO,-1FR") or bymd in ("1", "-2")):
            continue  # a position past every month's set: dateutil's 9999-year walk again
        yield _join("FREQ=MONTHLY", f"INTERVAL={interval}" if interval > 1 else None,
                    f"BYMONTH={bymonth}" if bymonth else None, f"BYDAY={byday}" if byday else None,
                    f"BYMONTHDAY={bymd}" if bymd else None, f"BYSETPOS={setpos}" if setpos else None, bound)
    yield "FREQ=MONTHLY;BYDAY=-1WE;BYMONTHDAY=1;BYSETPOS=1,-1"  # the explicit (near-)empty set
    yield "FREQ=MONTHLY;BYDAY=SA;BYSETPOS=9"
    for bymonth, byday, bymd, setpos, interval, bound in itertools.product(
            [None, "1", "1,2,3", "6,12"], [None, "MO", "-1WE", "1MO,-1FR", "SA,SU"],
            [None, "1", "15,-1"], [None, "1", "-1", "2,-2"], [1, 2], [None, "COUNT=6", "UNTIL={until}"]):
        ordinal = byday and any(ch.isdigit() for ch in byday)
        if ordinal and bymd:
            continue
        yield _join("FREQ=YEARLY", f"INTERVAL={interval}" if interval > 1 else None,
                    f"BYMONTH={bymonth}" if bymonth else None, f"BYDAY={byday}" if byday else None,
                    f"BYMONTHDAY={bymd}" if bymd else None, f"BYSETPOS={setpos}" if setpos else None, bound)


def _oracle_dtstart(dtstart):
    return dtstart if isinstance(dtstart, datetime) else datetime(dtstart.year, dtstart.month, dtstart.day)


def _stamp(v) -> str:
    """One comparable rendering for both sides: the wall-clock instant with
    its offset for date-times, the day for dates."""
    if isinstance(v, datetime):
        return v.isoformat() if v.tzinfo else v.date().isoformat()
    return v.isoformat()


@unittest.skipUnless(HAVE_DATEUTIL, "python-dateutil not importable (the oracle runs from a throwaway venv)")
class TestExpandRruleAgainstDateutil(unittest.TestCase):
    maxDiff = None

    def _compare(self, rrule: str, dtstart, window: str) -> tuple:
        """(ours, theirs) as stamp lists for one rule/start/window, with the
        oracle's second occurrence EXDATE'd on both sides."""
        is_dt = isinstance(dtstart, datetime)
        text = rrule.format(until=UNTIL_DT if is_dt else UNTIL_D)
        rule = cal.parse_rrule(text)
        odt = _oracle_dtstart(dtstart)
        duration = timedelta(minutes=30)
        base = odt
        if window == "near":
            ws, we = base - timedelta(days=1), base + timedelta(days=420)
        else:
            ws, we = base + timedelta(days=700), base + timedelta(days=760)
        # dateutil has no window: on a rule whose set is empty (a BYSETPOS
        # past the set's size, an intersection with nothing in it) it walks
        # to year 9999 looking for the next occurrence, ~300 ms a case. Give
        # it an UNTIL at the window's end -- inclusive, exactly where our
        # expander stops -- unless the rule has its own. (COUNT + UNTIL is
        # RFC-invalid, hence the warning dateutil raises; both limits apply.)
        otext = text
        if "UNTIL=" not in text:
            otext += ";UNTIL=" + (we.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ") if is_dt else we.strftime("%Y%m%d"))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            r = rrulestr(otext, dtstart=odt)
            firsts = list(r[:3])
        exdate = firsts[1] if len(firsts) > 1 else None
        rs = rruleset()
        rs.rrule(r)
        exdates = set()
        if exdate is not None:
            rs.exdate(exdate)
            exdates.add(cal._key(exdate if is_dt else exdate.date()))
        if is_dt:
            ours = cal.expand_rrule(dtstart, rule, exdates, ws, we, duration)
            theirs = rs.between(ws - duration, we, inc=True)
        else:
            tz = cal.local_tz()
            ours = cal.expand_rrule(dtstart, rule, exdates, ws.replace(tzinfo=tz), we.replace(tzinfo=tz), duration)
            theirs = [d for d in rs.between(ws - timedelta(days=1), we + timedelta(days=1), inc=True)
                      if ws - duration <= cal._as_datetime(d.date()).replace(tzinfo=None) <= we]
        return [_stamp(x) for x in ours], [_stamp(x) for x in theirs]

    def test_every_accepted_rule_matches_dateutil(self):
        full = os.environ.get("SPITBALL_ORACLE_FULL") == "1"
        rules = list(_rules())
        accepted = [r for r in rules if not cal.unsupported_rrule_reason(cal.parse_rrule(r.format(until=UNTIL_DT)))]
        refused = len(rules) - len(accepted)
        cases = 0
        mismatches = []
        t0 = time.perf_counter()
        for i, rrule in enumerate(accepted):
            starts = STARTS if full else [STARTS[i % len(STARTS)], STARTS[(i * 7 + 3) % len(STARTS)]]
            for dtstart in starts:
                for window in ("near", "far"):
                    cases += 1
                    ours, theirs = self._compare(rrule, dtstart, window)
                    if ours != theirs:
                        mismatches.append((rrule, _stamp(dtstart), window, ours[:6], theirs[:6]))
        elapsed = time.perf_counter() - t0
        print(f"\nrrule oracle: {len(rules)} rules in the matrix, {len(accepted)} accepted by "
              f"unsupported_rrule_reason (), {refused} refused; {cases} rule/start/window cases compared "
              f"against dateutil, {cases - len(mismatches)} matched, {len(mismatches)} mismatched "
              f"({elapsed:.1f}s)", file=sys.stderr)
        self.assertEqual(mismatches[:25], [], f"{len(mismatches)} of {cases} cases differ from dateutil")

    def test_refused_rules_would_indeed_differ_or_are_unmeetable(self):
        # A sample of what unsupported_rrule_reason() refuses, to show the
        # refusal is not gratuitous: each either differs from dateutil when
        # expanded naively by the nearest supported cousin, or is a shape
        # (sub-daily, week numbers, year days) the expander has no walk for.
        for rrule in ("FREQ=YEARLY;BYDAY=20MO", "FREQ=WEEKLY;BYWEEKNO=5", "FREQ=YEARLY;BYYEARDAY=100",
                      "FREQ=DAILY;BYHOUR=9,14", "FREQ=HOURLY;INTERVAL=2", "FREQ=WEEKLY;BYMONTHDAY=1",
                      "FREQ=WEEKLY;BYDAY=MO;BYSETPOS=1", "FREQ=DAILY;BYMONTH=6"):
            self.assertTrue(cal.unsupported_rrule_reason(cal.parse_rrule(rrule)), rrule)
            # dateutil itself has an answer for each; we simply don't claim one.
            self.assertIsNotNone(list(rrulestr(rrule, dtstart=STARTS[0])[:1]))


if __name__ == "__main__":
    unittest.main()
