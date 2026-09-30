"""`spitball check ...`, `spitball local ...`, and `spitball pick-folder` CLI
tests -- the settings panel's read-side backend surface. Provider/voxtype/
gdbus calls are mocked throughout."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spitball import __main__ as cli
from tests.testutil import isolated_runtime


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp = Path(self.tmpdir.name)
        self._rt_cm = isolated_runtime(self.tmp)
        self.config = self._rt_cm.__enter__()
        self.addCleanup(self._rt_cm.__exit__, None, None, None)

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()


class TestCheckTranscription(CliTestCase):
    def test_uses_configured_provider_by_default(self):
        with mock.patch("spitball.providers.check", return_value={"ok": True, "message": "fine"}) as m:
            code, out, _ = self.run_main(["check", "transcription", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"ok": True, "message": "fine"})
        m.assert_called_once()
        self.assertIsNone(m.call_args.args[1] if len(m.call_args.args) > 1 else m.call_args.kwargs.get("provider"))

    def test_provider_flag_overrides(self):
        with mock.patch("spitball.providers.check", return_value={"ok": False, "message": "bad"}) as m:
            code, out, _ = self.run_main(["check", "transcription", "--provider", "deepgram", "--json"])
        self.assertEqual(code, 1)
        args, kwargs = m.call_args
        self.assertIn("deepgram", args if len(args) > 1 else [kwargs.get("provider")])

    def test_unknown_provider_reports_error_not_crash(self):
        code, out, _ = self.run_main(["check", "transcription", "--provider", "bogus", "--json"])
        self.assertEqual(code, 1)
        parsed = json.loads(out)
        self.assertFalse(parsed["ok"])


class TestCheckSummary(CliTestCase):
    def test_reports_ok_and_models(self):
        with mock.patch("spitball.process.check_summary",
                         return_value={"ok": True, "message": "Reached x", "models": ["m1", "m2"]}):
            code, out, _ = self.run_main(["check", "summary", "--json"])
        self.assertEqual(code, 0)
        parsed = json.loads(out)
        self.assertEqual(parsed["models"], ["m1", "m2"])

    def test_failure_nonzero_exit(self):
        with mock.patch("spitball.process.check_summary",
                         return_value={"ok": False, "message": "down", "models": []}):
            code, out, _ = self.run_main(["check", "summary", "--json"])
        self.assertEqual(code, 1)


class TestLocalInfo(CliTestCase):
    def test_prints_info_json(self):
        meta = {"installed": True, "engine": "whisper", "model": "base.en", "onnx": False,
                 "can_upgrade_parakeet": True, "message": "Using voxtype: Whisper base.en"}
        with mock.patch("spitball.providers.local.info", return_value=meta):
            code, out, _ = self.run_main(["local", "info"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), meta)


class TestLocalModels(CliTestCase):
    def test_prints_models_json(self):
        models = [{"name": "base.en", "engine": "whisper", "installed": True, "size_mb": 148,
                    "languages": "English", "recommended": False, "active": True}]
        with mock.patch("spitball.providers.local.list_models", return_value=models):
            code, out, _ = self.run_main(["local", "models"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), models)


class TestLocalSetModel(CliTestCase):
    def test_background_success(self):
        result = {"ok": True, "mode": "background", "message": "switching to parakeet-tdt-0.6b-v3-int8 in the background"}
        with mock.patch("spitball.providers.local.set_model", return_value=result) as m:
            code, out, _ = self.run_main(["local", "set-model", "parakeet-tdt-0.6b-v3-int8"])
        self.assertEqual(code, 0)
        m.assert_called_once_with("parakeet-tdt-0.6b-v3-int8")
        self.assertIn("parakeet-tdt-0.6b-v3-int8", out)

    def test_terminal_fallback_success(self):
        result = {"ok": True, "mode": "terminal", "message": "opened a terminal to switch voxtype to base.en"}
        with mock.patch("spitball.providers.local.set_model", return_value=result):
            code, out, _ = self.run_main(["local", "set-model", "base.en"])
        self.assertEqual(code, 0)
        self.assertIn("terminal", out)

    def test_nothing_available_is_a_failure(self):
        result = {"ok": False, "mode": "none", "message": "no graphical password helper and no terminal launcher available"}
        with mock.patch("spitball.providers.local.set_model", return_value=result):
            code, _, err = self.run_main(["local", "set-model", "base.en"])
        self.assertEqual(code, 1)
        self.assertIn("no graphical password helper", err)

    def test_missing_arg_prints_usage(self):
        code, out, _ = self.run_main(["local", "set-model"])
        self.assertEqual(code, 2)
        self.assertIn("usage", out)


class TestLocalSetModelWorkerCli(CliTestCase):
    """`local _set-model-worker <name>` is the internal command the detached
    child process set_model() spawns runs -- never invoked interactively."""

    def test_runs_the_worker_and_returns_0(self):
        with mock.patch("spitball.providers.local.run_set_model_worker") as m:
            code, out, _ = self.run_main(["local", "_set-model-worker", "base.en"])
        self.assertEqual(code, 0)
        m.assert_called_once_with("base.en")

    def test_missing_arg_prints_usage(self):
        code, out, _ = self.run_main(["local", "_set-model-worker"])
        self.assertEqual(code, 2)
        self.assertIn("usage", out)


class TestPickFolderCli(CliTestCase):
    def test_prints_path_on_success(self):
        with mock.patch("spitball.pickfolder.pick_folder", return_value=(0, "/home/morgan/Calls")) as m:
            code, out, _ = self.run_main(["pick-folder", "--title", "Pick it"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "/home/morgan/Calls")
        m.assert_called_once_with("Pick it")

    def test_default_title(self):
        with mock.patch("spitball.pickfolder.pick_folder", return_value=(2, "")) as m:
            code, out, _ = self.run_main(["pick-folder"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        m.assert_called_once_with("Choose a folder")

    def test_canceled_prints_nothing_exit_1(self):
        with mock.patch("spitball.pickfolder.pick_folder", return_value=(1, "")):
            code, out, _ = self.run_main(["pick-folder"])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")


class TestReprocessRetranscribe(CliTestCase):
    def test_retranscribe_flag_forwarded(self):
        call_dir = self.tmp / "Calls" / "call-1"
        call_dir.mkdir(parents=True)
        (call_dir / "audio.opus").write_bytes(b"x")
        with mock.patch("spitball.process.process",
                         return_value={"title": "T", "summary": str(call_dir / "summary.md")}) as pm:
            code, out, _ = self.run_main(["reprocess", str(call_dir), "--retranscribe"])
        self.assertEqual(code, 0)
        _, kwargs = pm.call_args
        self.assertTrue(kwargs.get("retranscribe"))

    def test_without_flag_defaults_false(self):
        call_dir = self.tmp / "Calls" / "call-1"
        call_dir.mkdir(parents=True)
        with mock.patch("spitball.process.process",
                         return_value={"title": "T", "summary": "x"}) as pm:
            self.run_main(["reprocess", str(call_dir)])
        _, kwargs = pm.call_args
        self.assertFalse(kwargs.get("retranscribe"))


if __name__ == "__main__":
    unittest.main()
