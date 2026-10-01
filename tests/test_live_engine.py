"""spitball.live_engine tests. The real worker (onnx-asr + a 2.4 GB model)
never runs here: the protocol is exercised against small fake worker
scripts run under this same interpreter, and tests/__init__.py points
SPITBALL_ENGINE_DIR at a session temp dir with no venv in it."""
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from spitball import live_engine


def _fake_worker(tmp: Path, body: str) -> Path:
    script = tmp / "fake_worker.py"
    script.write_text(textwrap.dedent('''
        import json, sys
        def say(o):
            sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
    ''') + textwrap.dedent(body))
    return script


ECHO_WORKER = '''
    say({"ready": True, "model": "fake", "load_s": 0})
    for line in sys.stdin:
        req = json.loads(line)
        if req["wav"].endswith("bad.wav"):
            say({"id": req["id"], "error": "cannot read"})
        elif req["wav"].endswith("quit.wav"):
            sys.exit(0)
        else:
            say({"id": req["id"], "text": "heard " + req["wav"].rsplit("/", 1)[-1]})
'''


class TestOpenEngine(unittest.TestCase):
    def test_not_installed_in_tests(self):
        self.assertFalse(live_engine.installed())
        self.assertIsNone(live_engine.open_engine({}))

    def test_setting_off_skips_even_when_installed(self):
        with mock.patch("spitball.live_engine.installed", return_value=True), \
             mock.patch("spitball.live_engine.Engine.open") as opener:
            self.assertIsNone(live_engine.open_engine({"live_engine": False}))
        opener.assert_not_called()

    def test_whisper_model_stays_on_voxtype(self):
        with mock.patch("spitball.live_engine.installed", return_value=True), \
             mock.patch("spitball.providers.local.info", return_value={"engine": "whisper", "model": "base.en"}), \
             mock.patch("spitball.live_engine.Engine.open") as opener:
            self.assertIsNone(live_engine.open_engine({}))
        opener.assert_not_called()


class TestInstalled(unittest.TestCase):
    """installed(): Codex P2 regression -- it used to be "the venv python
    exists", so a venv `spitball diarize setup` made (sherpa-onnx only)
    read as a working live engine: `live status` said installed, the Live
    page hid Install, and the worker could not start."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        p = mock.patch("spitball.live_engine.ENGINE_DIR", self.tmp)
        p.start()
        self.addCleanup(p.stop)
        self.venv = self.tmp / "venv"

    def _bare_venv(self):
        (self.venv / "bin").mkdir(parents=True)
        (self.venv / "bin" / "python").write_text("")

    def test_no_venv(self):
        self.assertFalse(live_engine.venv_present())
        self.assertFalse(live_engine.installed())
        self.assertFalse(live_engine.has_package("onnx_asr"))

    def test_bare_or_diarize_only_venv_is_not_installed(self):
        self._bare_venv()
        self.assertTrue(live_engine.venv_present())
        self.assertFalse(live_engine.installed())
        (self.venv / "lib" / "python3.14" / "site-packages" / "sherpa_onnx").mkdir(parents=True)
        self.assertFalse(live_engine.installed())
        with mock.patch("spitball.live_engine.Engine.open") as opener:
            self.assertIsNone(live_engine.open_engine({}))
        opener.assert_not_called()

    def test_onnx_asr_in_the_venv_means_installed(self):
        self._bare_venv()
        (self.venv / "lib" / "python3.14" / "site-packages" / "onnx_asr").mkdir(parents=True)
        self.assertTrue(live_engine.installed())
        # A stray file of that name is not a package.
        shutil.rmtree(self.venv / "lib" / "python3.14" / "site-packages" / "onnx_asr")
        (self.venv / "lib" / "python3.14" / "site-packages" / "onnx_asr").write_text("")
        self.assertFalse(live_engine.installed())

    def test_setup_into_a_diarize_only_venv_installs_onnx_asr(self):
        # `live setup` must not skip an existing venv: it adds its packages.
        self._bare_venv()
        (self.venv / "lib" / "python3.14" / "site-packages" / "sherpa_onnx").mkdir(parents=True)
        run = mock.Mock(return_value=mock.Mock(returncode=0, stdout="", stderr=""))
        with mock.patch("spitball.live_engine.shutil.which", return_value="/usr/bin/uv"):
            ok, _ = live_engine.setup(run=run)
        self.assertTrue(ok)
        cmds = [c.args[0] for c in run.call_args_list]
        self.assertIn("--allow-existing", cmds[0])
        self.assertEqual(cmds[1][:3], ["/usr/bin/uv", "pip", "install"])
        self.assertTrue(any(p.startswith("onnx-asr") for p in cmds[1]), cmds[1])

    def test_live_status_says_why_when_the_venv_has_no_engine(self):
        import contextlib
        import io
        import json
        from spitball.__main__ import main
        self._bare_venv()
        with mock.patch("spitball.providers.local.info", return_value={"engine": "parakeet", "model": ""}):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(["live", "status"]), 0)
            self.assertIn("has no onnx-asr", out.getvalue())
            self.assertIn("spitball live setup", out.getvalue())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                main(["live", "status", "--json"])
            status = json.loads(out.getvalue())
            self.assertEqual((status["installed"], status["fast"]), (False, False))
            self.assertEqual(status["venv"], str(self.venv))
        shutil.rmtree(self.venv)
        with mock.patch("spitball.providers.local.info", return_value={"engine": "parakeet", "model": ""}):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                main(["live", "status"])
            self.assertEqual(out.getvalue().strip(), "Fast live transcript: not installed (run `spitball live setup`)")


class TestModelDirFor(unittest.TestCase):
    def test_parakeet_model_dir_when_present(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "parakeet-unified-en-0.6b").mkdir()
            with mock.patch("spitball.providers.local.MODEL_DIR", Path(d)):
                got = live_engine.model_dir_for({"engine": "parakeet", "model": "parakeet-unified-en-0.6b"})
                missing = live_engine.model_dir_for({"engine": "parakeet", "model": "not-downloaded"})
        self.assertEqual(got.name, "parakeet-unified-en-0.6b")
        self.assertIsNone(missing)

    def test_non_parakeet_is_none(self):
        self.assertIsNone(live_engine.model_dir_for({"engine": "whisper", "model": "base.en"}))
        self.assertIsNone(live_engine.model_dir_for({}))


class TestEngineProtocol(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def open(self, body: str, timeout: float = 10):
        return live_engine.Engine.open(self.tmp, python=Path(sys.executable),
                                       script=_fake_worker(self.tmp, body), timeout=timeout)

    def test_round_trip(self):
        engine = self.open(ECHO_WORKER)
        self.assertIsNotNone(engine)
        self.addCleanup(engine.close)
        self.assertEqual(engine.transcribe(Path("/x/one.wav")), "heard one.wav")
        self.assertEqual(engine.transcribe(Path("/x/two.wav")), "heard two.wav")

    def test_request_error_returns_none_but_worker_stays_up(self):
        engine = self.open(ECHO_WORKER)
        self.addCleanup(engine.close)
        self.assertIsNone(engine.transcribe(Path("/x/bad.wav")))
        self.assertTrue(engine.alive())
        self.assertEqual(engine.transcribe(Path("/x/ok.wav")), "heard ok.wav")

    def test_worker_exit_returns_none_and_closes(self):
        engine = self.open(ECHO_WORKER)
        self.assertIsNone(engine.transcribe(Path("/x/quit.wav")))
        self.assertFalse(engine.alive())

    def test_startup_error_means_no_engine(self):
        self.assertIsNone(self.open('say({"error": "no model"})\n'))

    def test_worker_that_never_gets_ready_times_out(self):
        self.assertIsNone(self.open("import time; time.sleep(30)\n", timeout=0.5))

    def test_stray_output_before_ready_is_ignored(self):
        engine = self.open('print("some library banner", flush=True)\n' + textwrap.dedent(ECHO_WORKER))
        self.assertIsNotNone(engine)
        self.addCleanup(engine.close)
        self.assertEqual(engine.transcribe(Path("/x/a.wav")), "heard a.wav")


class TestSetup(unittest.TestCase):
    def test_uses_uv_when_available(self):
        run = mock.Mock(return_value=mock.Mock(returncode=0, stdout="", stderr=""))
        with mock.patch("spitball.live_engine.shutil.which", return_value="/usr/bin/uv"):
            ok, _ = live_engine.setup(run=run)
        self.assertTrue(ok)
        cmds = [c.args[0] for c in run.call_args_list]
        self.assertEqual(cmds[0][:2], ["/usr/bin/uv", "venv"])
        self.assertEqual(cmds[1][:3], ["/usr/bin/uv", "pip", "install"])
        self.assertTrue(set(live_engine.PACKAGES) <= set(cmds[1]))

    def test_falls_back_to_stdlib_venv_and_pip(self):
        run = mock.Mock(return_value=mock.Mock(returncode=0, stdout="", stderr=""))
        with mock.patch("spitball.live_engine.shutil.which", return_value=None):
            ok, _ = live_engine.setup(run=run)
        self.assertTrue(ok)
        cmds = [c.args[0] for c in run.call_args_list]
        self.assertEqual(cmds[0][:3], ["/usr/bin/python3", "-m", "venv"])
        self.assertIn("pip", cmds[1])

    def test_failure_reports_last_line(self):
        run = mock.Mock(return_value=mock.Mock(returncode=1, stdout="", stderr="resolving\nno wheel for onnxruntime\n"))
        with mock.patch("spitball.live_engine.shutil.which", return_value="/usr/bin/uv"):
            ok, message = live_engine.setup(run=run)
        self.assertFalse(ok)
        self.assertEqual(message, "no wheel for onnxruntime")


if __name__ == "__main__":
    unittest.main()


class TestInstallHelper(unittest.TestCase):
    """install()/venv_steps(): shared with spitball/diarize.py, which puts
    its add-on in this same venv."""

    def test_install_returns_empty_on_success(self):
        run = mock.Mock(return_value=mock.Mock(returncode=0, stdout="", stderr=""))
        with mock.patch("spitball.live_engine.shutil.which", return_value="/usr/bin/uv"):
            self.assertEqual(live_engine.install(("sherpa-onnx",), run=run), "")
        cmds = [c.args[0] for c in run.call_args_list]
        self.assertIn("sherpa-onnx", cmds[1])
        self.assertNotIn("sentencepiece", cmds[1])

    def test_install_reports_the_error(self):
        run = mock.Mock(side_effect=OSError("no uv here"))
        with mock.patch("spitball.live_engine.shutil.which", return_value="/usr/bin/uv"):
            self.assertIn("no uv here", live_engine.install(("x",), run=run))
