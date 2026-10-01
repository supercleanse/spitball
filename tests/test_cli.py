"""spitball.__main__ CLI tests: every command via main([...]) against a real
Daemon.serve() thread bound to an isolated temp socket. Never touches the
live daemon's ctl.sock -- isolated_runtime() points config.SOCKET_PATH (and
everything else) at a temp dir before the Daemon or the CLI ever look at it.
"""
import contextlib
import io
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from spitball import __main__ as cli
from tests.testutil import isolated_runtime, make_cfg, make_fake_recording, track_threads


def wait_for_socket(path: Path, timeout=5):
    """True once something is actually accepting connections at `path` --
    not just once the path exists. Daemon.serve() creates the path with
    bind() one line before listen(); a bare path.exists() check can win that
    race and get ECONNREFUSED, so this confirms with a real connect."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(str(path))
                return True
            except OSError:
                pass
            finally:
                probe.close()
        time.sleep(0.02)
    return False


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp = Path(self.tmpdir.name)
        self._rt_cm = isolated_runtime(self.tmp)
        self.config = self._rt_cm.__enter__()
        self.addCleanup(self._rt_cm.__exit__, None, None, None)
        # Real notify-send would pop a real desktop notification; never let
        # tick()/start()/stop() shell out to it during CLI tests.
        self._notify_patch = mock.patch("spitball.daemon.notify")
        self._notify_patch.start()
        self.addCleanup(self._notify_patch.stop)

    def start_daemon(self, **cfg_overrides):
        from spitball.daemon import Daemon
        d = Daemon()
        d.cfg = make_cfg(self.tmp, **cfg_overrides)
        thread = threading.Thread(target=d.serve, daemon=True)
        thread.start()
        self.assertTrue(wait_for_socket(self.config.SOCKET_PATH), "daemon socket never appeared")
        return d, thread

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()


class TestHelp(CliTestCase):
    def test_no_args_prints_usage(self):
        code, out, _ = self.run_main([])
        self.assertEqual(code, 0)
        self.assertIn("usage: spitball", out)

    def test_dash_h(self):
        code, out, _ = self.run_main(["-h"])
        self.assertEqual(code, 0)
        self.assertIn("usage: spitball", out)

    def test_dash_dash_help(self):
        code, out, _ = self.run_main(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("usage: spitball", out)

    def test_help_word(self):
        code, out, _ = self.run_main(["help"])
        self.assertEqual(code, 0)
        self.assertIn("usage: spitball", out)

    def test_unknown_command_prints_usage_and_exit_2(self):
        code, out, _ = self.run_main(["bogus-command"])
        self.assertEqual(code, 2)
        self.assertIn("usage: spitball", out)


class TestDaemonUnreachable(CliTestCase):
    def test_status_when_nothing_is_listening(self):
        code, out, err = self.run_main(["status"])
        self.assertEqual(code, 1)
        self.assertIn("daemon not reachable", out)

    def test_start_when_nothing_is_listening(self):
        code, out, err = self.run_main(["start"])
        self.assertEqual(code, 1)
        self.assertIn("daemon not reachable", err)


class TestStatusCommand(CliTestCase):
    def test_status_text(self):
        d, thread = self.start_daemon(detect_after_s=0, end_after_s=5)
        with mock.patch("spitball.daemon.detect.call_apps", return_value={"Zoom"}):
            d.tick()
        code, out, _ = self.run_main(["status"])
        self.assertEqual(code, 0)
        self.assertIn("detected", out)
        self.assertIn("Zoom", out)

    def test_status_json(self):
        d, thread = self.start_daemon()
        code, out, _ = self.run_main(["status", "--json"])
        self.assertEqual(code, 0)
        parsed = json.loads(out)
        self.assertEqual(parsed["state"], "idle")
        self.assertTrue(parsed["ok"])


class TestStartStopToggleDismissAuto(CliTestCase):
    def test_start_then_stop(self):
        d, thread = self.start_daemon(min_manual_s=1)
        rec = make_fake_recording(alive=True, stop_duration=999)
        with mock.patch("spitball.daemon.Recording", return_value=rec):
            code, out, _ = self.run_main(["start"])
        self.assertEqual(code, 0)
        self.assertEqual(d.state, "recording")

        with track_threads() as threads, \
             mock.patch("spitball.process.process",
                        return_value={"dir": str(d.rec_dir), "title": "T",
                                       "summary": str(d.rec_dir / "summary.md"), "ended_at": 1}):
            code, out, _ = self.run_main(["stop"])
            # stop() only starts the background _process() thread and returns;
            # join it here, still inside the mock's scope, so a straggler can
            # never race past this test's teardown and hit the real pipeline.
            for t in threads:
                t.join(timeout=5)
        self.assertEqual(code, 0)

    def test_toggle(self):
        d, thread = self.start_daemon(min_manual_s=1)
        rec = make_fake_recording(alive=True, stop_duration=999)
        with mock.patch("spitball.daemon.Recording", return_value=rec):
            code, _, _ = self.run_main(["toggle"])
        self.assertEqual(code, 0)
        self.assertEqual(d.state, "recording")

    def test_dismiss(self):
        d, thread = self.start_daemon(detect_after_s=0, end_after_s=5)
        with mock.patch("spitball.daemon.detect.call_apps", return_value={"Zoom"}):
            d.tick()
        code, _, _ = self.run_main(["dismiss"])
        self.assertEqual(code, 0)
        self.assertEqual(d.state, "idle")

    def test_auto_on_off_toggle(self):
        d, thread = self.start_daemon()
        code, _, _ = self.run_main(["auto", "on"])
        self.assertEqual(code, 0)
        self.assertTrue(d.auto_record)
        code, _, _ = self.run_main(["auto", "off"])
        self.assertEqual(code, 0)
        self.assertFalse(d.auto_record)
        code, _, _ = self.run_main(["auto", "toggle"])
        self.assertEqual(code, 0)
        self.assertTrue(d.auto_record)


class TestOpenLastAndFolder(CliTestCase):
    def test_open_last_with_no_calls_yet(self):
        d, thread = self.start_daemon()
        code, out, _ = self.run_main(["open-last"])
        self.assertEqual(code, 1)
        self.assertIn("no calls yet", out)

    def test_open_last_opens_existing_summary(self):
        d, thread = self.start_daemon()
        summary = self.tmp / "Calls" / "call-1" / "summary.md"
        summary.parent.mkdir(parents=True)
        summary.write_text("# T")
        d.last_call = {"dir": str(summary.parent), "title": "T", "ended_at": 1, "summary": str(summary)}
        with mock.patch("spitball.__main__._open") as open_mock:
            code, _, _ = self.run_main(["open-last"])
        self.assertEqual(code, 0)
        open_mock.assert_called_once_with(str(summary))

    def test_open_folder_creates_and_opens_calls_dir(self):
        config_file = self.tmp / "cfg" / "config.json"
        config_file.parent.mkdir(parents=True)
        calls_dir = self.tmp / "Calls"
        config_file.write_text(json.dumps({"calls_dir": str(calls_dir)}))
        with mock.patch.object(cli.config, "CONFIG_FILE", config_file), \
             mock.patch("spitball.__main__._open") as open_mock:
            code, _, _ = self.run_main(["open-folder"])
        self.assertEqual(code, 0)
        self.assertTrue(calls_dir.is_dir())
        open_mock.assert_called_once_with(str(calls_dir))


class TestReprocess(CliTestCase):
    def test_reprocess_calls_process_pipeline(self):
        call_dir = self.tmp / "Calls" / "call-1"
        call_dir.mkdir(parents=True)
        (call_dir / "audio.opus").write_bytes(b"x")
        (call_dir / ".meta.json").write_text(json.dumps(
            {"app": "Zoom", "started_at": 1000.0, "duration": 90.0}))
        with mock.patch("spitball.process.process",
                        return_value={"title": "Reprocessed", "summary": str(call_dir / "summary.md")}) as pm:
            code, out, _ = self.run_main(["reprocess", str(call_dir)])
        self.assertEqual(code, 0)
        self.assertIn("Reprocessed", out)
        pm.assert_called_once()

    def test_reprocess_without_arg_prints_usage(self):
        code, out, _ = self.run_main(["reprocess"])
        self.assertEqual(code, 2)
        self.assertIn("usage: spitball", out)


if __name__ == "__main__":
    unittest.main()


class TestCalendarCommand(CliTestCase):
    """`spitball calendar test`: reads config.json (isolated), runs the
    configured source (a `calendar_command` here -- no network), prints the
    match. Exit 0 when the source works, 1 when it doesn't or isn't set."""

    CMD = ("python3 -c \"import json; print(json.dumps([{'title': 'Weekly sync', "
           "'start': '2026-09-30T14:00:00-06:00', 'end': '2026-09-30T14:30:00-06:00', "
           "'attendees': [{'name': 'Alex Demo', 'email': 'alex@example.com'}], "
           "'conference': 'https://meet.google.com/abc-defg-hij'}]))\"")

    def _write_config(self, **keys):
        self.config.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.config.CONFIG_FILE.write_text(json.dumps(keys))

    def test_nothing_configured_exits_1_with_json(self):
        code, out, _ = self.run_main(["calendar", "test", "--json"])
        self.assertEqual(code, 1)
        data = json.loads(out)
        self.assertFalse(data["ok"])
        self.assertEqual(data["source"], "off")
        self.assertIn("secret iCal address", data["message"])

    def test_match_at_a_given_time_with_meet_code(self):
        self._write_config(calendar_source="command", calendar_command=self.CMD)
        code, out, _ = self.run_main(["calendar", "test", "--at", "2026-09-30T20:03:00Z", "--meet", "ABC-DEFG-HIJ",
                                      "--app", "Chrome", "--json"])
        self.assertEqual(code, 0, out)
        data = json.loads(out)
        self.assertTrue(data["ok"])
        self.assertEqual(data["match"]["title"], "Weekly sync")
        self.assertTrue(data["confident"])
        self.assertGreaterEqual(data["confidence"], 100)
        self.assertEqual(data["events_nearby"], 1)
        code, out, _ = self.run_main(["calendar", "test", "--at", "2026-09-30T20:03:00Z", "--meet", "abc-defg-hij"])
        self.assertEqual(code, 0)
        self.assertIn("Match: Weekly sync", out)
        self.assertIn("Attendees: Alex Demo", out)
        # Without the Meet code a lone 1:1 with no response of mine stays a weak match.
        code, out, _ = self.run_main(["calendar", "test", "--at", "2026-09-30T20:03:00Z"])
        self.assertEqual(code, 0)
        self.assertIn("Match: none", out)
        self.assertIn("Candidates:", out)

    def test_no_events_is_still_ok(self):
        self._write_config(calendar_source="command", calendar_command=self.CMD)
        code, out, _ = self.run_main(["calendar", "test", "--at", "2027-01-01T20:03:00Z", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertTrue(data["ok"])
        self.assertIsNone(data["match"])
        self.assertEqual(data["summary"], "no events at that time")

    def test_bad_at_value(self):
        code, _, err = self.run_main(["calendar", "test", "--at", "sometime"])
        self.assertEqual(code, 2)
        self.assertIn("bad --at", err)

    def test_broken_command_exits_1(self):
        self._write_config(calendar_source="command", calendar_command="exit 7")
        code, out, _ = self.run_main(["calendar", "test", "--json"])
        self.assertEqual(code, 1)
        self.assertIn("exited 7", json.loads(out)["message"])

    def test_unknown_subcommand_prints_usage(self):
        code, out, _ = self.run_main(["calendar", "wat"])
        self.assertEqual(code, 2)
        self.assertIn("usage:", out)


class TestReprocessCalendarFlags(CliTestCase):
    def test_no_event_and_event_flags_set_the_override(self):
        call_dir = self.tmp / "call"
        call_dir.mkdir()
        (call_dir / ".meta.json").write_text(json.dumps({"app": "", "started_at": 1, "duration": 2}))
        with mock.patch("spitball.process.process", return_value={"title": "T", "summary": "s"}) as proc:
            code, _, _ = self.run_main(["reprocess", str(call_dir), "--no-event"])
        self.assertEqual(code, 0)
        proc.assert_called_once()
        self.assertEqual(proc.call_args.args[0], call_dir.resolve())
        self.assertEqual(json.loads((call_dir / ".meta.json").read_text())["calendar"]["override"], {"event": None})
        with mock.patch("spitball.process.process", return_value={"title": "T", "summary": "s"}) as proc:
            code, _, _ = self.run_main(["reprocess", "--event", "abc@x", str(call_dir), "--retranscribe"])
        self.assertEqual(code, 0)
        self.assertEqual(proc.call_args.args[0], call_dir.resolve())
        self.assertTrue(proc.call_args.kwargs["retranscribe"])
        self.assertEqual(json.loads((call_dir / ".meta.json").read_text())["calendar"]["override"], {"event": "abc@x"})

    def test_event_and_no_event_together_is_usage(self):
        code, out, _ = self.run_main(["reprocess", "/tmp/x", "--event", "a", "--no-event"])
        self.assertEqual(code, 2)
        self.assertIn("usage:", out)


class TestSpeakersAndDiarizeCommands(CliTestCase):
    """`spitball speakers <dir> [n "Name" | --clear] [--json]` and `spitball
    diarize setup|status`. The processing side is real (a cached transcript
    and an existing summary in a temp call folder); no model, no worker."""

    def _call_dir(self):
        call_dir = self.tmp / "Calls" / "2026-09-28-1400-zoom-weekly"
        call_dir.mkdir(parents=True)
        (call_dir / "audio.opus").write_bytes(b"x")
        (call_dir / ".meta.json").write_text(json.dumps(
            {"app": "Zoom", "started_at": 1790000000.0, "duration": 90.0, "titled": True}))
        (call_dir / ".transcript.json").write_text(json.dumps({"provider": "deepgram", "model": "nova-3", "utterances": [
            {"channel": 1, "speaker": 2, "start": 0.0, "end": 8.0, "transcript": "first voice with a good many words in it here"},
            {"channel": 1, "speaker": 5, "start": 9.0, "end": 17.0, "transcript": "second voice with a good many words in it too"},
            {"channel": 0, "speaker": 0, "start": 18.0, "end": 19.0, "transcript": "ok"}]}))
        (call_dir / "summary.md").write_text("# Weekly\n\n## Summary\n- Speaker 2 will wait.\n\n---\n\n**Date:** x\n")
        return call_dir

    def test_list_text_and_json(self):
        call_dir = self._call_dir()
        code, out, _ = self.run_main(["speakers", str(call_dir)])
        self.assertEqual(code, 0)
        self.assertIn("1  Speaker 1  (unnamed)", out)
        self.assertIn("2  Speaker 2  (unnamed)", out)
        code, out, _ = self.run_main(["speakers", str(call_dir), "--json"])
        self.assertEqual(code, 0)
        rows = json.loads(out)
        self.assertEqual([r["label"] for r in rows], ["Speaker 1", "Speaker 2"])

    def test_rename_then_clear(self):
        call_dir = self._call_dir()
        code, out, _ = self.run_main(["speakers", str(call_dir), "2", "Priya Nair"])
        self.assertEqual(code, 0, out)
        self.assertIn("2  Speaker 2  Priya Nair (user, high)", out)
        self.assertIn("**[00:00:09] Priya Nair:**", (call_dir / "transcript.md").read_text())
        self.assertIn("- Priya Nair will wait.", (call_dir / "summary.md").read_text())
        cache = json.loads((call_dir / ".transcript.json").read_text())
        self.assertEqual(cache["speakers"]["2"]["source"], "user")
        code, out, _ = self.run_main(["speakers", str(call_dir), "2", "--clear"])
        self.assertEqual(code, 0, out)
        self.assertIn("2  Speaker 2  (unnamed)", out)
        self.assertIn("**[00:00:09] Speaker 2:**", (call_dir / "transcript.md").read_text())

    def test_errors(self):
        call_dir = self._call_dir()
        code, out, err = self.run_main(["speakers", str(call_dir), "7", "Nobody"])
        self.assertEqual(code, 1)
        self.assertIn("no far-side speaker 7", err)
        code, out, err = self.run_main(["speakers", str(self.tmp / "nope")])
        self.assertEqual(code, 1)
        self.assertIn("no cached transcript", err)
        code, out, _ = self.run_main(["speakers"])
        self.assertEqual(code, 2)
        code, out, _ = self.run_main(["speakers", str(call_dir), "2"])  # a number with no name and no --clear
        self.assertEqual(code, 2)
        code, out, _ = self.run_main(["speakers", str(call_dir), "two", "Name"])
        self.assertEqual(code, 2)

    def test_diarize_status_text_and_json(self):
        code, out, _ = self.run_main(["diarize", "status"])
        self.assertEqual(code, 0)
        self.assertIn("Speaker split: not installed", out)
        code, out, _ = self.run_main(["diarize", "status", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertFalse(data["installed"])
        self.assertEqual(data["engine"], "sherpa-onnx")
        with mock.patch("spitball.diarize.status", return_value={"installed": True, "package": True, "models": True,
                                                                  "engine": "sherpa-onnx", "model_dir": "/m", "venv": "/v"}):
            code, out, _ = self.run_main(["diarize", "status"])
        self.assertIn("Speaker split: installed (sherpa-onnx, models in /m)", out)
        with mock.patch("spitball.diarize.status", return_value={"installed": False, "package": True, "models": False,
                                                                  "engine": "sherpa-onnx", "model_dir": "/m", "venv": "/v"}):
            code, out, _ = self.run_main(["diarize", "status"])
        self.assertIn("models are missing", out)

    def test_diarize_setup_reports_result(self):
        with mock.patch("spitball.diarize.setup", return_value=(True, "Speaker split installed")):
            code, out, _ = self.run_main(["diarize", "setup"])
        self.assertEqual(code, 0)
        self.assertIn("Speaker split installed", out)
        with mock.patch("spitball.diarize.setup", return_value=(False, "no wheel")):
            code, _, err = self.run_main(["diarize", "setup"])
        self.assertEqual(code, 1)
        self.assertIn("no wheel", err)

    def test_diarize_unknown_subcommand(self):
        code, out, _ = self.run_main(["diarize", "frobnicate"])
        self.assertEqual(code, 2)
        self.assertIn("usage: spitball", out)


class TestCalendarUpcomingCommand(CliTestCase):
    """`spitball calendar upcoming`: the reminders due from now, from the
    configured source (a `calendar_command` here, whose one event starts
    sixty seconds from the moment it runs -- no network)."""

    CMD = ("python3 -c \"import json,time; from datetime import datetime, timezone; "
           "s=datetime.fromtimestamp(time.time()+60, tz=timezone.utc); e=datetime.fromtimestamp(time.time()+1860, tz=timezone.utc); "
           "print(json.dumps([{'title': 'Weekly sync', 'start': s.isoformat(), 'end': e.isoformat(), "
           "'location': 'https://zoom.us/j/555'}, {'title': 'Lunch', 'start': s.isoformat(), 'end': e.isoformat()}]))\"")

    def _write_config(self, **keys):
        self.config.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.config.CONFIG_FILE.write_text(json.dumps(keys))

    def test_nothing_configured_exits_1_with_json(self):
        code, out, _ = self.run_main(["calendar", "upcoming", "--json"])
        self.assertEqual(code, 1)
        data = json.loads(out)
        self.assertFalse(data["ok"])
        self.assertEqual(data["source"], "off")

    def test_lists_the_reminder_json_and_text(self):
        self._write_config(calendar_enabled=True, calendar_source="command", calendar_command=self.CMD)
        code, out, _ = self.run_main(["calendar", "upcoming", "--json", "--hours", "2"])
        self.assertEqual(code, 0, out)
        data = json.loads(out)
        self.assertTrue(data["active"])
        self.assertEqual(data["lead_s"], 60)
        self.assertEqual([u["title"] for u in data["upcoming"]], ["Weekly sync"])
        self.assertEqual(data["upcoming"][0]["host"], "zoom.us")
        self.assertEqual(data["skipped"], 1)
        code, out, _ = self.run_main(["calendar", "upcoming"])
        self.assertEqual(code, 0)
        self.assertIn("Reminders: on; 60 s before; source: command", out)
        self.assertIn("Weekly sync  (zoom.us)", out)
        self.assertIn("Skipped: 1 event ", out)

    def test_reminders_off_still_lists(self):
        self._write_config(calendar_enabled=True, calendar_source="command", calendar_command=self.CMD,
                           calendar_reminders=False, calendar_remind_before_s=120)
        code, out, _ = self.run_main(["calendar", "upcoming", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertFalse(data["active"])
        self.assertEqual(data["lead_s"], 120)
        self.assertEqual(len(data["upcoming"]), 1)

    def test_bad_hours(self):
        code, _, err = self.run_main(["calendar", "upcoming", "--hours", "lots"])
        self.assertEqual(code, 2)
        self.assertIn("bad --hours", err)
        code, _, err = self.run_main(["calendar", "upcoming", "--hours", "0"])
        self.assertEqual(code, 2)

    def test_broken_command_exits_1(self):
        self._write_config(calendar_enabled=True, calendar_source="command", calendar_command="exit 7")
        code, out, _ = self.run_main(["calendar", "upcoming", "--json"])
        self.assertEqual(code, 1)
        self.assertIn("exited 7", json.loads(out)["error"])
