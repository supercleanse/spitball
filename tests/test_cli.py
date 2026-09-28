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
