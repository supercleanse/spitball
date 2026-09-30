"""spitball.providers.local tests. voxtype's own subprocess calls (config get,
info models, transcribe, setup onnx --status) are mocked throughout; ffmpeg
segmenting (via spitball.audio) is left real where it's easy to (it's
already a hard Spitball dependency, same reasoning as test_audio.py), with a
selective fake standing in only for the `voxtype ...` calls in the mix.
Never runs a real `sudo`, `pkexec`, voxtype download, or the real
bin/spitball-upgrade-parakeet script.
"""
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spitball.providers import local

_REAL_RUN = subprocess.run


class TestCleanStdout(unittest.TestCase):
    def test_strips_header_lines(self):
        raw = ('Loading audio file: "x.wav"\n'
               "Audio format: 16000 Hz, 1 channel(s), Int\n"
               "Processing 16000 samples (1.00s)...\n"
               "\n"
               "hello there\n")
        self.assertEqual(local._clean_stdout(raw), "hello there")

    def test_joins_multiple_text_lines(self):
        raw = "Loading audio file: x\n\nfirst line\nsecond line\n"
        self.assertEqual(local._clean_stdout(raw), "first line second line")

    def test_empty_transcript(self):
        raw = "Loading audio file: x\nAudio format: x\nProcessing 1 samples...\n\n"
        self.assertEqual(local._clean_stdout(raw), "")


class TestInfo(unittest.TestCase):
    def test_not_installed(self):
        with mock.patch("shutil.which", return_value=None):
            meta = local.info()
        self.assertEqual(meta, {"installed": False, "engine": "", "model": "", "onnx": False,
                                 "can_upgrade_parakeet": False, "message": local.NOT_INSTALLED_MESSAGE})

    def test_installed_whisper(self):
        def fake_run(cmd, **kw):
            if cmd[-1] == "engine":
                return mock.Mock(stdout="whisper\n")
            if cmd[-1] == "whisper.model":
                return mock.Mock(stdout="base.en\n")
            if cmd[:3] == ["voxtype", "setup", "onnx"]:
                return mock.Mock(stdout="Active engine: Whisper\n  Binary: /usr/lib/voxtype/voxtype-avx2\n")
            return mock.Mock(stdout="")
        with mock.patch("shutil.which", return_value="/usr/bin/voxtype"), \
             mock.patch("spitball.providers.local.subprocess.run", side_effect=fake_run), \
             mock.patch("spitball.providers.local._onnx_binaries_present", return_value=True):
            meta = local.info()
        self.assertTrue(meta["installed"])
        self.assertEqual(meta["engine"], "whisper")
        self.assertEqual(meta["model"], "base.en")
        self.assertFalse(meta["onnx"])
        self.assertTrue(meta["can_upgrade_parakeet"])
        self.assertIn("Whisper base.en", meta["message"])

    def test_installed_parakeet_onnx_active(self):
        def fake_run(cmd, **kw):
            if cmd[-1] == "engine":
                return mock.Mock(stdout="parakeet\n")
            if cmd[-1] == "parakeet.model":
                return mock.Mock(stdout="parakeet-tdt-0.6b-v3-int8\n")
            if cmd[:3] == ["voxtype", "setup", "onnx"]:
                # voxtype's real output for the ONNX build (captured on Omarchy).
                return mock.Mock(stdout="=== Voxtype ONNX Engine Status ===\n\n"
                                        "Active engine: Parakeet\n  Backend: ONNX (AVX2)\n")
            return mock.Mock(stdout="")
        with mock.patch("shutil.which", return_value="/usr/bin/voxtype"), \
             mock.patch("spitball.providers.local.subprocess.run", side_effect=fake_run), \
             mock.patch("spitball.providers.local._onnx_binaries_present", return_value=True):
            meta = local.info()
        self.assertEqual(meta["engine"], "parakeet")
        self.assertTrue(meta["onnx"])
        self.assertFalse(meta["can_upgrade_parakeet"])  # already parakeet -- nothing to upgrade to

    def test_subprocess_failure_falls_back_to_empty(self):
        with mock.patch("shutil.which", return_value="/usr/bin/voxtype"), \
             mock.patch("spitball.providers.local.subprocess.run", side_effect=OSError("nope")):
            meta = local.info()
        self.assertTrue(meta["installed"])
        self.assertEqual(meta["engine"], "")
        self.assertEqual(meta["model"], "")


class TestReadyAndCheck(unittest.TestCase):
    def test_ready_when_installed(self):
        with mock.patch("shutil.which", return_value="/usr/bin/voxtype"):
            self.assertEqual(local.ready({}), "")

    def test_not_ready_when_missing(self):
        with mock.patch("shutil.which", return_value=None):
            self.assertEqual(local.ready({}), local.NOT_INSTALLED_MESSAGE)

    def test_check_reflects_info(self):
        with mock.patch("spitball.providers.local.info",
                         return_value={"installed": True, "engine": "whisper", "model": "base.en",
                                       "onnx": False, "can_upgrade_parakeet": True, "message": "Using voxtype: Whisper base.en"}):
            result = local.check({})
        self.assertTrue(result["ok"])
        self.assertIn("Whisper", result["message"])

    def test_check_not_installed(self):
        with mock.patch("spitball.providers.local.info",
                         return_value={"installed": False, "engine": "", "model": "", "onnx": False,
                                       "can_upgrade_parakeet": False, "message": local.NOT_INSTALLED_MESSAGE}):
            result = local.check({})
        self.assertFalse(result["ok"])


class TestListModels(unittest.TestCase):
    CATALOG = {
        "engines": {
            "whisper": {"default": "base.en", "models": [
                {"name": "tiny", "installed": False},
                {"name": "base.en", "installed": True},
            ]},
            "parakeet": {"default": "parakeet-tdt-0.6b-v3", "models": [
                {"name": "parakeet-tdt-0.6b-v3-int8", "installed": True},
                {"name": "parakeet-tdt-0.6b-v2", "installed": False},
                {"name": "parakeet-unified-en-0.6b", "installed": False},
            ]},
            "moonshine": {"default": "base", "models": [{"name": "base", "installed": False}]},
        }
    }

    def test_only_whisper_and_parakeet_included(self):
        with mock.patch("shutil.which", return_value="/usr/bin/voxtype"), \
             mock.patch("spitball.providers.local.subprocess.run",
                        return_value=mock.Mock(stdout=json.dumps(self.CATALOG))), \
             mock.patch("spitball.providers.local.info",
                        return_value={"engine": "whisper", "model": "base.en"}), \
             mock.patch("spitball.providers.local._installed_size_mb", return_value=100):
            models = local.list_models()
        engines = {m["engine"] for m in models}
        self.assertEqual(engines, {"whisper", "parakeet"})
        self.assertEqual(len(models), 5)

    def test_active_flag_matches_current_engine_and_model(self):
        with mock.patch("shutil.which", return_value="/usr/bin/voxtype"), \
             mock.patch("spitball.providers.local.subprocess.run",
                        return_value=mock.Mock(stdout=json.dumps(self.CATALOG))), \
             mock.patch("spitball.providers.local.info",
                        return_value={"engine": "whisper", "model": "base.en"}), \
             mock.patch("spitball.providers.local._installed_size_mb", return_value=100):
            models = local.list_models()
        active = [m for m in models if m["active"]]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["name"], "base.en")

    def test_recommended_model_flagged(self):
        with mock.patch("shutil.which", return_value="/usr/bin/voxtype"), \
             mock.patch("spitball.providers.local.subprocess.run",
                        return_value=mock.Mock(stdout=json.dumps(self.CATALOG))), \
             mock.patch("spitball.providers.local.info", return_value={"engine": "", "model": ""}), \
             mock.patch("spitball.providers.local._installed_size_mb", return_value=100):
            models = local.list_models()
        recommended = [m["name"] for m in models if m["recommended"]]
        self.assertEqual(recommended, ["parakeet-unified-en-0.6b"])

    def test_language_labels(self):
        with mock.patch("shutil.which", return_value="/usr/bin/voxtype"), \
             mock.patch("spitball.providers.local.subprocess.run",
                        return_value=mock.Mock(stdout=json.dumps(self.CATALOG))), \
             mock.patch("spitball.providers.local.info", return_value={"engine": "", "model": ""}), \
             mock.patch("spitball.providers.local._installed_size_mb", return_value=100):
            models = local.list_models()
        by_name = {m["name"]: m["languages"] for m in models}
        self.assertEqual(by_name["tiny"], "~99 languages")
        self.assertEqual(by_name["base.en"], "English")
        self.assertEqual(by_name["parakeet-tdt-0.6b-v3-int8"], "25 European languages")
        self.assertEqual(by_name["parakeet-tdt-0.6b-v2"], "English")

    def test_no_voxtype_is_empty_list(self):
        with mock.patch("shutil.which", return_value=None):
            self.assertEqual(local.list_models(), [])

    def test_garbage_json_is_empty_list(self):
        with mock.patch("shutil.which", return_value="/usr/bin/voxtype"), \
             mock.patch("spitball.providers.local.subprocess.run",
                        return_value=mock.Mock(stdout="not json")):
            self.assertEqual(local.list_models(), [])


class TestInstalledSizeMb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_whisper_file_size(self):
        with mock.patch("spitball.providers.local.MODEL_DIR", self.dir):
            (self.dir / "ggml-base.en.bin").write_bytes(b"x" * 1048576)  # exactly 1 MiB
            self.assertEqual(local._installed_size_mb("whisper", "base.en"), 1)

    def test_parakeet_dir_size_sums_files(self):
        with mock.patch("spitball.providers.local.MODEL_DIR", self.dir):
            d = self.dir / "parakeet-tdt-0.6b-v3-int8"
            d.mkdir()
            (d / "encoder.onnx").write_bytes(b"x" * 1048576)
            (d / "decoder.onnx").write_bytes(b"x" * 1048576)
            self.assertEqual(local._installed_size_mb("parakeet", "parakeet-tdt-0.6b-v3-int8"), 2)

    def test_missing_returns_none(self):
        with mock.patch("spitball.providers.local.MODEL_DIR", self.dir):
            self.assertIsNone(local._installed_size_mb("whisper", "nope"))
            self.assertIsNone(local._installed_size_mb("parakeet", "nope"))


class TestTranscribe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.audio = Path(self.tmp.name) / "stereo.wav"
        import shutil as _shutil
        if not (_shutil.which("ffmpeg") and _shutil.which("ffprobe")):
            self.skipTest("ffmpeg/ffprobe not installed")
        os.system(
            "ffmpeg -hide_banner -loglevel error "
            f"-f lavfi -i \"sine=frequency=440:duration=2\" "
            f"-f lavfi -i \"sine=frequency=880:duration=2\" "
            "-filter_complex \"[0:a][1:a]amerge=inputs=2[a]\" -map \"[a]\" -ac 2 "
            f"{self.audio}")

    def test_not_installed_raises(self):
        with mock.patch("shutil.which", return_value=None):
            with self.assertRaises(RuntimeError) as cm:
                local.transcribe(self.audio, {})
        self.assertIn("not installed", str(cm.exception))

    def test_transcribes_both_channels_with_fake_voxtype(self):
        real_run = subprocess.run

        def fake_run(cmd, **kw):
            if cmd[0] == "voxtype":
                return mock.Mock(stdout="Loading audio file: x\n\nfake transcript text\n", returncode=0)
            return real_run(cmd, **kw)

        with mock.patch("shutil.which", return_value="/usr/bin/voxtype"), \
             mock.patch("spitball.providers.local.subprocess.run", side_effect=fake_run), \
             mock.patch("spitball.providers.local.info", return_value={"model": "base.en"}):
            result = local.transcribe(self.audio, {})
        self.assertEqual(result["provider"], "local")
        self.assertEqual(result["model"], "base.en")
        # A continuous 2s tone on each channel, no silence -- one window per channel.
        channels = {u["channel"] for u in result["utterances"]}
        self.assertEqual(channels, {0, 1})
        for u in result["utterances"]:
            self.assertEqual(u["transcript"], "fake transcript text")

    def test_no_speech_windows_yields_no_utterances(self):
        # A silent file: silencedetect reports the whole thing as one giant
        # silence, so speech_windows() finds nothing to transcribe.
        silent = Path(self.tmp.name) / "silent.wav"
        os.system(f"ffmpeg -hide_banner -loglevel error -f lavfi -i anullsrc -t 2 -ac 2 {silent}")
        with mock.patch("shutil.which", return_value="/usr/bin/voxtype"), \
             mock.patch("spitball.providers.local.info", return_value={"model": "base.en"}):
            result = local.transcribe(silent, {})
        self.assertEqual(result["utterances"], [])


class TestTerminalCommand(unittest.TestCase):
    def test_engine_for_model_name(self):
        self.assertEqual(local._engine_for_model("parakeet-tdt-0.6b-v3-int8"), "parakeet")
        self.assertEqual(local._engine_for_model("base.en"), "whisper")

    def test_prefers_omarchy_launcher(self):
        with mock.patch("shutil.which", side_effect=lambda n: "/x" if n == "omarchy-launch-floating-terminal-with-presentation" else None):
            cmd = local._resolve_terminal_command(Path("/script"), ["model", "whisper"])
        self.assertEqual(cmd[0], "omarchy-launch-floating-terminal-with-presentation")
        self.assertIn("model", cmd[1])

    def test_falls_back_to_xdg_terminal_exec(self):
        with mock.patch("shutil.which", side_effect=lambda n: "/x" if n == "xdg-terminal-exec" else None):
            cmd = local._resolve_terminal_command(Path("/script"), ["model", "whisper"])
        self.assertEqual(cmd[0], "xdg-terminal-exec")

    def test_falls_back_to_terminal_env(self):
        with mock.patch("shutil.which", side_effect=lambda n: "/x" if n == "foo-term" else None), \
             mock.patch.dict(os.environ, {"TERMINAL": "foo-term"}):
            cmd = local._resolve_terminal_command(Path("/script"), ["model", "whisper"])
        self.assertEqual(cmd[0], "foo-term")

    def test_no_terminal_available_returns_none(self):
        with mock.patch("shutil.which", return_value=None), \
             mock.patch.dict(os.environ, {}, clear=True):
            cmd = local._resolve_terminal_command(Path("/script"), ["model", "whisper"])
        self.assertIsNone(cmd)


class TestPolkitAgentLikelyAvailable(unittest.TestCase):
    def test_true_when_shell_ping_succeeds(self):
        runner = mock.Mock(return_value=mock.Mock(returncode=0))
        self.assertTrue(local._polkit_agent_likely_available(ping_runner=runner))
        runner.assert_called_once_with(["omarchy-shell", "-q", "shell", "ping"],
                                        capture_output=True, text=True, timeout=5)

    def test_false_when_shell_ping_fails(self):
        runner = mock.Mock(return_value=mock.Mock(returncode=1))
        self.assertFalse(local._polkit_agent_likely_available(ping_runner=runner))

    def test_false_on_oserror(self):
        runner = mock.Mock(side_effect=OSError("no such command"))
        self.assertFalse(local._polkit_agent_likely_available(ping_runner=runner))


class ModelStateTestCase(unittest.TestCase):
    """Base for anything that touches model.json -- always isolated, never
    the real $XDG_RUNTIME_DIR/spitball."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        from tests.testutil import isolated_runtime
        self._rt_cm = isolated_runtime(Path(self.tmpdir.name))
        self._rt_cm.__enter__()
        self.addCleanup(self._rt_cm.__exit__, None, None, None)


class TestSetModel(ModelStateTestCase):
    """`set_model()` never runs pkexec/sudo/a real download itself -- it
    only ever decides between two detached launches (the background worker,
    or the terminal fallback) and returns immediately."""

    def test_background_path_when_pkexec_and_agent_available(self):
        popen_calls = []

        def fake_popen(cmd, **kw):
            popen_calls.append((cmd, kw))
            return mock.Mock()

        with mock.patch("shutil.which", side_effect=lambda n: "/usr/bin/pkexec" if n == "pkexec" else None), \
             mock.patch("spitball.providers.local.info", return_value={"engine": "whisper"}):
            result = local.set_model("parakeet-tdt-0.6b-v3-int8", popen=fake_popen,
                                      ping_runner=mock.Mock(return_value=mock.Mock(returncode=0)))
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "background")
        self.assertEqual(len(popen_calls), 1)
        cmd, kw = popen_calls[0]
        self.assertNotIn("sudo", cmd)
        self.assertNotIn("pkexec", cmd)  # spawned re-exec, not pkexec itself
        self.assertIn("_set-model-worker", cmd)
        self.assertIn("parakeet-tdt-0.6b-v3-int8", cmd)
        self.assertTrue(kw.get("start_new_session"))
        state = local.read_model_state()
        self.assertEqual(state["state"], "switching-engine")  # whisper -> parakeet is an engine change

    def test_background_path_skips_engine_step_when_engine_unchanged(self):
        with mock.patch("shutil.which", side_effect=lambda n: "/usr/bin/pkexec" if n == "pkexec" else None), \
             mock.patch("spitball.providers.local.info", return_value={"engine": "whisper"}):
            local.set_model("base.en", popen=mock.Mock(),
                             ping_runner=mock.Mock(return_value=mock.Mock(returncode=0)))
        self.assertEqual(local.read_model_state()["state"], "downloading")

    def test_falls_back_to_terminal_when_pkexec_missing(self):
        popen_calls = []

        def fake_popen(cmd, **kw):
            popen_calls.append((cmd, kw))
            return mock.Mock()

        with mock.patch("shutil.which",
                         side_effect=lambda n: "/x" if n == "xdg-terminal-exec" else None):
            result = local.set_model("base.en", popen=fake_popen,
                                      ping_runner=mock.Mock(return_value=mock.Mock(returncode=0)))
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "terminal")
        self.assertEqual(len(popen_calls), 1)
        cmd, kw = popen_calls[0]
        self.assertNotIn("sudo", cmd)
        self.assertTrue(kw.get("start_new_session"))
        state = local.read_model_state()
        self.assertEqual(state["state"], "terminal")

    def test_falls_back_to_terminal_when_no_polkit_agent(self):
        with mock.patch("shutil.which",
                         side_effect=lambda n: "/usr/bin/pkexec" if n == "pkexec"
                         else ("/x" if n == "xdg-terminal-exec" else None)):
            result = local.set_model("base.en", popen=mock.Mock(),
                                      ping_runner=mock.Mock(return_value=mock.Mock(returncode=1)))
        self.assertEqual(result["mode"], "terminal")

    def test_no_pkexec_and_no_terminal_reports_failure_without_launching_anything(self):
        with mock.patch("shutil.which", return_value=None), \
             mock.patch.dict(os.environ, {}, clear=True):
            result = local.set_model("base.en", popen=mock.Mock(),
                                      ping_runner=mock.Mock(return_value=mock.Mock(returncode=0)))
        self.assertFalse(result["ok"])
        self.assertEqual(result["mode"], "none")
        # Nothing written -- there's no switch of any kind actually in flight.
        self.assertIsNone(local.read_model_state())


class TestRunSetModelWorker(ModelStateTestCase):
    """The blocking worker `set_model()` re-execs into. `run`/`popen` are
    always mocked -- this never runs a real pkexec, sudo, or voxtype
    download."""

    def _fake_download_popen(self, events, returncode=0):
        proc = mock.Mock()
        proc.stdout = [json.dumps(e) + "\n" for e in events]
        proc.stderr = io.StringIO("")
        proc.wait.return_value = returncode
        return mock.Mock(return_value=proc)

    def test_success_no_engine_change_no_restart(self):
        run = mock.Mock(side_effect=[mock.Mock(returncode=1)])  # systemctl is-active: not active
        popen = self._fake_download_popen([
            {"event": "progress", "bytes": 50, "total": 100},
            {"event": "progress", "bytes": 100, "total": 100},
            {"event": "done"},
        ])
        with mock.patch("spitball.providers.local.info", return_value={"engine": "whisper"}):
            local.run_set_model_worker("base.en", run=run, popen=popen)
        state = local.read_model_state()
        self.assertEqual(state["state"], "done")
        self.assertEqual(state["name"], "base.en")
        run.assert_called_once()  # only the is-active check -- no restart, no pkexec

    def test_success_with_engine_change_and_restart(self):
        run = mock.Mock(side_effect=[
            mock.Mock(returncode=0),  # pkexec voxtype setup onnx --enable
            mock.Mock(returncode=0),  # voxtype config set parakeet.streaming false
            mock.Mock(returncode=0),  # systemctl is-active: active
            mock.Mock(returncode=0),  # systemctl restart
        ])
        popen = self._fake_download_popen([{"event": "done"}])
        with mock.patch("spitball.providers.local.info", return_value={"engine": "whisper"}):
            local.run_set_model_worker("parakeet-tdt-0.6b-v3-int8", run=run, popen=popen)
        self.assertEqual(local.read_model_state()["state"], "done")
        self.assertEqual(run.call_count, 4)
        pkexec_call = run.call_args_list[0]
        self.assertEqual(pkexec_call.args[0], ["pkexec", "voxtype", "setup", "onnx", "--enable"])
        streaming_call = run.call_args_list[1]
        self.assertEqual(streaming_call.args[0],
                         ["voxtype", "config", "set", "parakeet.streaming", "false"])
        restart_call = run.call_args_list[3]
        self.assertEqual(restart_call.args[0], ["systemctl", "--user", "restart", "voxtype"])

    def test_streaming_model_turns_streaming_on_and_writes_windows(self):
        run = mock.Mock(side_effect=[
            mock.Mock(returncode=0),  # voxtype config set parakeet.streaming true
            mock.Mock(returncode=1),  # systemctl is-active: not active
        ])
        popen = self._fake_download_popen([{"event": "done"}])
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.toml"
            cfg.write_text('engine = "parakeet"\n\n[parakeet]\nmodel = "parakeet-unified-en-0.6b"\n')
            with mock.patch("spitball.providers.local.info", return_value={"engine": "parakeet"}), \
                 mock.patch("spitball.providers.local.voxtype_config_path", return_value=cfg):
                local.run_set_model_worker("parakeet-unified-en-0.6b", run=run, popen=popen)
            text = cfg.read_text()
        self.assertEqual(local.read_model_state()["state"], "done")
        self.assertEqual(run.call_args_list[0].args[0],
                         ["voxtype", "config", "set", "parakeet.streaming", "true"])
        for key, value in local.STREAMING_WINDOWS:
            self.assertIn(f"{key} = {value}", text)

    def test_streaming_config_failure_reports_error(self):
        run = mock.Mock(side_effect=[mock.Mock(returncode=1, stderr="error: bad key\n")])
        popen = self._fake_download_popen([{"event": "done"}])
        with mock.patch("spitball.providers.local.info", return_value={"engine": "parakeet"}):
            local.run_set_model_worker("parakeet-tdt-0.6b-v3-int8", run=run, popen=popen)
        state = local.read_model_state()
        self.assertEqual(state["state"], "error")
        self.assertEqual(state["message"], "error: bad key")

    def test_whisper_switch_leaves_streaming_alone(self):
        run = mock.Mock(side_effect=[mock.Mock(returncode=1)])  # systemctl is-active only
        popen = self._fake_download_popen([{"event": "done"}])
        with mock.patch("spitball.providers.local.info", return_value={"engine": "whisper"}):
            local.run_set_model_worker("base.en", run=run, popen=popen)
        run.assert_called_once()

    def test_engine_change_to_whisper_disables_onnx(self):
        run = mock.Mock(side_effect=[mock.Mock(returncode=0), mock.Mock(returncode=1)])
        popen = self._fake_download_popen([{"event": "done"}])
        with mock.patch("spitball.providers.local.info", return_value={"engine": "parakeet"}):
            local.run_set_model_worker("base.en", run=run, popen=popen)
        pkexec_call = run.call_args_list[0]
        self.assertEqual(pkexec_call.args[0], ["pkexec", "voxtype", "setup", "onnx", "--disable"])

    def test_pkexec_canceled_reports_password_prompt_canceled(self):
        run = mock.Mock(return_value=mock.Mock(returncode=126, stderr=""))
        with mock.patch("spitball.providers.local.info", return_value={"engine": "whisper"}):
            local.run_set_model_worker("parakeet-tdt-0.6b-v3-int8", run=run, popen=mock.Mock())
        state = local.read_model_state()
        self.assertEqual(state["state"], "error")
        self.assertEqual(state["message"], "Password prompt canceled")

    def test_pkexec_other_failure_surfaces_stderr(self):
        run = mock.Mock(return_value=mock.Mock(returncode=1, stderr="voxtype: not found\n"))
        with mock.patch("spitball.providers.local.info", return_value={"engine": "whisper"}):
            local.run_set_model_worker("parakeet-tdt-0.6b-v3-int8", run=run, popen=mock.Mock())
        state = local.read_model_state()
        self.assertEqual(state["state"], "error")
        self.assertEqual(state["message"], "voxtype: not found")

    def test_download_error_event_reported(self):
        run = mock.Mock()
        popen = self._fake_download_popen(
            [{"event": "error", "message": "Unknown model 'bogus'."}], returncode=1)
        with mock.patch("spitball.providers.local.info", return_value={"engine": "whisper"}):
            local.run_set_model_worker("bogus", run=run, popen=popen)
        state = local.read_model_state()
        self.assertEqual(state["state"], "error")
        self.assertEqual(state["message"], "Unknown model 'bogus'.")
        run.assert_not_called()  # never reaches the systemctl step

    def test_progress_events_update_done_and_total_bytes(self):
        run = mock.Mock(return_value=mock.Mock(returncode=1))
        popen = self._fake_download_popen([
            {"event": "progress", "bytes": 25, "total": 100},
        ], returncode=0)
        # Patch _write_model_state to capture every intermediate write, since
        # read_model_state() only ever sees the last one.
        writes = []
        real_write = local._write_model_state

        def spy(name, state, message="", done_bytes=0, total_bytes=0):
            writes.append((state, done_bytes, total_bytes))
            real_write(name, state, message, done_bytes, total_bytes)

        with mock.patch("spitball.providers.local.info", return_value={"engine": "whisper"}), \
             mock.patch("spitball.providers.local._write_model_state", side_effect=spy):
            local.run_set_model_worker("base.en", run=run, popen=popen)
        progress_writes = [w for w in writes if w[0] == "downloading"]
        self.assertIn(("downloading", 25, 100), progress_writes)

    def test_unexpected_exception_still_writes_an_error_state(self):
        with mock.patch("spitball.providers.local.info", side_effect=RuntimeError("boom")):
            local.run_set_model_worker("base.en", run=mock.Mock(), popen=mock.Mock())
        state = local.read_model_state()
        self.assertEqual(state["state"], "error")
        self.assertIn("boom", state["message"])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe not installed")
class TestMicDenoiseInTranscribe(unittest.TestCase):
    """docs/SPEC-v2.md section 3: the local provider denoises a temp copy of
    the mic channel per `mic_denoise`, records what ran, and (on Whisper)
    segments with the tighter speech detection. voxtype is faked; ffmpeg is
    real."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.audio = self.dir / "audio.opus"
        # Channel 0: a 2 s tone at full scale (floor -3 dBFS: "noisy" by any
        # threshold); channel 1: a quieter tone. No pauses anywhere.
        os.system(
            "ffmpeg -hide_banner -loglevel error "
            f"-f lavfi -i \"sine=frequency=440:duration=2\" "
            f"-f lavfi -i \"sine=frequency=880:duration=2\" "
            "-filter_complex \"[0:a][1:a]amerge=inputs=2[a]\" -map \"[a]\" -ac 2 "
            f"{self.audio}")
        self.calls = []

    def _fake_run(self, cmd, **kw):
        if cmd[0] == "voxtype":
            self.calls.append(Path(cmd[-1]))
            return mock.Mock(stdout="Loading audio file: x\n\ntext\n", returncode=0)
        return _REAL_RUN(cmd, **kw)

    def _transcribe(self, cfg, engine="parakeet"):
        with mock.patch("shutil.which", return_value="/usr/bin/voxtype"), \
             mock.patch("spitball.providers.local.subprocess.run", side_effect=self._fake_run), \
             mock.patch("spitball.providers.local.info",
                        return_value={"engine": engine, "model": "m"}):
            return local.transcribe(self.audio, cfg)

    def test_default_auto_records_the_decision(self):
        result = self._transcribe({})
        rec = result["mic_denoise"]
        self.assertEqual(rec["mode"], "auto")
        self.assertTrue(rec["applied"])          # a full-scale tone sits far above -45
        self.assertEqual(rec["filter"], "arnndn")
        self.assertGreater(rec["noise_floor_db"], -45.0)
        self.assertEqual(rec["threshold_db"], -45.0)
        self.assertEqual({u["channel"] for u in result["utterances"]}, {0, 1})

    def test_off_never_touches_the_mic_copy(self):
        with mock.patch("spitball.denoise.apply") as apply:
            result = self._transcribe({"mic_denoise": "off"})
        apply.assert_not_called()
        self.assertEqual(result["mic_denoise"]["mode"], "off")
        self.assertFalse(result["mic_denoise"]["applied"])
        self.assertIsNone(result["mic_denoise"]["filter"])

    def test_on_transcribes_the_denoised_copy(self):
        seen = {}

        def fake_apply(src, dst, model_path=None):
            seen["src"], seen["dst"] = src, dst
            shutil.copy(src, dst)
            return "arnndn"

        with mock.patch("spitball.denoise.apply", side_effect=fake_apply):
            result = self._transcribe({"mic_denoise": "on"})
        self.assertEqual(seen["src"].name, "channel-0.wav")
        self.assertEqual(seen["dst"].name, "channel-0-denoised.wav")
        self.assertTrue(result["mic_denoise"]["applied"])
        # Channel 0's clips came out of the denoised copy, channel 1's never did.
        self.assertTrue(any(c.name.startswith("ch0-") for c in self.calls))
        self.assertTrue(any(c.name.startswith("ch1-") for c in self.calls))

    def test_auto_below_threshold_leaves_it_alone(self):
        with mock.patch("spitball.denoise.apply") as apply:
            result = self._transcribe({"mic_denoise": "auto", "mic_noise_floor_db": -20})
        apply.assert_not_called()
        self.assertFalse(result["mic_denoise"]["applied"])
        self.assertEqual(result["mic_denoise"]["threshold_db"], -20.0)

    def test_setting_is_read_on_every_call(self):
        # What `spitball reprocess --retranscribe` relies on: the provider
        # reads cfg each time, so flipping the key changes the next run.
        off = self._transcribe({"mic_denoise": "off"})["mic_denoise"]
        on = self._transcribe({"mic_denoise": "on"})["mic_denoise"]
        self.assertEqual((off["applied"], on["applied"]), (False, True))

    def test_recording_is_never_rewritten(self):
        before = self.audio.read_bytes()
        self._transcribe({"mic_denoise": "on"})
        self.assertEqual(self.audio.read_bytes(), before)

    def test_whisper_path_skips_noise_only_windows(self):
        # Channel 0: a 2 s tone near -9 dBFS RMS (ffmpeg's sine is 1/8 full
        # scale, hence +12 dB), then 35 s of steady pink noise near -30 dBFS
        # -- above silencedetect's -35 dB gate, so the plain segmentation
        # (Parakeet) cuts it into 30 s blocks and transcribes noise. The
        # Whisper path raises the gate to the floor and drops windows with
        # nothing above the room in them.
        audio_path = self.dir / "noisy.opus"
        os.system(
            "ffmpeg -hide_banner -loglevel error -y "
            "-filter_complex \""
            "sine=frequency=440:duration=2:sample_rate=16000,volume=12dB[t];"
            "anoisesrc=color=pink:sample_rate=16000:amplitude=0.2:seed=5,atrim=duration=35[n];"
            "[t][n]concat=n=2:v=0:a=1[mic];"
            "anullsrc=r=16000:cl=mono,atrim=duration=37[far];"
            "[mic][far]amerge=inputs=2[a]\" -map \"[a]\" -ac 2 -c:a libopus "
            f"{audio_path}")
        self.audio = audio_path
        cfg = {"mic_denoise": "off"}  # isolate the segmentation from the denoiser
        self._transcribe(cfg, engine="parakeet")
        parakeet_calls = [c for c in self.calls if c.name.startswith("ch0-")]
        self.calls = []
        result = self._transcribe(cfg, engine="whisper")
        whisper_calls = [c for c in self.calls if c.name.startswith("ch0-")]
        self.assertGreaterEqual(len(parakeet_calls), 2, parakeet_calls)
        self.assertEqual(len(whisper_calls), 1, whisper_calls)
        mic_utts = [u for u in result["utterances"] if u["channel"] == 0]
        self.assertEqual(len(mic_utts), 1)
        self.assertLessEqual(mic_utts[0]["start"], 0.2)
        self.assertGreaterEqual(mic_utts[0]["end"], 1.8)


if __name__ == "__main__":
    unittest.main()


ONNX_ERROR_STDOUT = ("Loading audio file: x\nProcessing 100 samples...\n\n"
                     "\x1b[2m2026-09-28T16:07:56.768911Z\x1b[0m \x1b[31mERROR\x1b[0m "
                     "Non-zero status code returned while running Add node.\n")


class TestVoxtypeFailures(unittest.TestCase):
    """Regression for a real call: an 8-minute mic window made Parakeet's ONNX
    runtime fail, and the error text landed in the transcript."""

    def test_clean_stdout_drops_log_lines(self):
        text = local._clean_stdout(ONNX_ERROR_STDOUT + "real words here\n")
        self.assertEqual(text, "real words here")

    def test_failed_on_nonzero_exit(self):
        self.assertTrue(local._failed(mock.Mock(returncode=1, stdout="text")))

    def test_failed_on_error_log_line_with_zero_exit(self):
        self.assertTrue(local._failed(mock.Mock(returncode=0, stdout=ONNX_ERROR_STDOUT)))

    def test_ok_output_not_failed(self):
        self.assertFalse(local._failed(mock.Mock(returncode=0, stdout="Loading audio file: x\nhello\n")))

    def _run_window(self, fake_run, start=0.0, end=40.0):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("spitball.providers.local.audio_mod.extract_clip"), \
             mock.patch("spitball.providers.local.subprocess.run", side_effect=fake_run):
            return local._transcribe_window(Path(tmp) / "in.wav", 0, start, end, Path(tmp), "w")

    def test_failure_retries_halves(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if len(calls) == 1:
                return mock.Mock(returncode=1, stdout=ONNX_ERROR_STDOUT)
            return mock.Mock(returncode=0, stdout=f"part {len(calls)}\n")

        utts = self._run_window(fake_run)
        self.assertEqual(len(calls), 3)
        self.assertEqual([(u["start"], u["end"]) for u in utts], [(0.0, 20.0), (20.0, 40.0)])
        self.assertTrue(all("ERROR" not in u["transcript"] for u in utts))
        self.assertFalse(any(u.get("failed") for u in utts))

    def test_persistent_failure_becomes_visible_marker(self):
        utts = self._run_window(lambda cmd, **kw: mock.Mock(returncode=1, stdout=ONNX_ERROR_STDOUT),
                                end=6.0)
        self.assertEqual(len(utts), 1)
        self.assertTrue(utts[0]["failed"])
        self.assertIn("transcription failed", utts[0]["transcript"])

    def test_timeout_is_treated_as_failure(self):
        def fake_run(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 300)
        utts = self._run_window(fake_run, end=5.0)
        self.assertTrue(utts[0]["failed"])


class TestEnsureStreamingWindows(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "config.toml"

    def tearDown(self):
        self._tmp.cleanup()

    def test_adds_missing_keys_inside_existing_table(self):
        self.path.write_text('[parakeet]\nmodel = "x"\n\n[output]\nmode = "type"\n')
        local._ensure_streaming_windows(self.path)
        text = self.path.read_text()
        parakeet = text.split("[output]")[0]
        for key, value in local.STREAMING_WINDOWS:
            self.assertIn(f"{key} = {value}", parakeet)
        self.assertIn('[output]\nmode = "type"\n', text)

    def test_keeps_user_values(self):
        self.path.write_text("[parakeet]\nstreaming_chunk_secs = 0.56\n")
        local._ensure_streaming_windows(self.path)
        text = self.path.read_text()
        self.assertIn("streaming_chunk_secs = 0.56", text)
        self.assertNotIn("streaming_chunk_secs = 1.12", text)
        self.assertIn("streaming_left_context_secs = 5.6", text)

    def test_creates_table_when_missing(self):
        self.path.write_text('engine = "parakeet"\n')
        local._ensure_streaming_windows(self.path)
        text = self.path.read_text()
        self.assertTrue(text.startswith('engine = "parakeet"\n\n[parakeet]\n'))

    def test_idempotent(self):
        self.path.write_text("[parakeet]\n")
        local._ensure_streaming_windows(self.path)
        once = self.path.read_text()
        local._ensure_streaming_windows(self.path)
        self.assertEqual(self.path.read_text(), once)
