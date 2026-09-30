"""`spitball config get/set/set-secret/unset` CLI tests: JSON typing, unknown
keys rejected, secrets never printed/settable via `set`/argv, mode 600,
reload sent (mocked here; test_cli.py-style live-daemon coverage lives in
TestConfigReloadReachesDaemon below), and -- the must-have from the task --
a `set` never drops keys this version of Spitball doesn't manage (the live
user config carries extras like deepgram_api_key_command/summary_base_url/
summary_model/export_dir/my_name that must all survive).
"""
import contextlib
import io
import json
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from spitball import __main__ as cli
from spitball import config
from tests.testutil import isolated_runtime


class ConfigCliTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp = Path(self.tmpdir.name)
        self._rt_cm = isolated_runtime(self.tmp)
        self.config = self._rt_cm.__enter__()
        self.addCleanup(self._rt_cm.__exit__, None, None, None)
        # No daemon running in most of these -- `send("reload")` should just
        # fail quietly ("daemon not reachable"), never raise.

    def run_main(self, argv, stdin_text=None):
        out, err = io.StringIO(), io.StringIO()
        stdin = io.StringIO(stdin_text) if stdin_text is not None else io.StringIO("")
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
             mock.patch("sys.stdin", stdin):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()


class TestConfigGet(ConfigCliTestCase):
    def test_defaults_when_no_file(self):
        code, out, _ = self.run_main(["config", "get", "--json"])
        self.assertEqual(code, 0)
        parsed = json.loads(out)
        self.assertEqual(parsed["calls_dir"], config.DEFAULTS["calls_dir"])
        self.assertEqual(parsed["transcription_provider"], "local")

    def test_secrets_masked_as_set_source(self):
        self.config.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.config.CONFIG_FILE.write_text(json.dumps({"deepgram_api_key": "top-secret-value"}))
        code, out, _ = self.run_main(["config", "get", "--json"])
        self.assertEqual(code, 0)
        self.assertNotIn("top-secret-value", out)
        parsed = json.loads(out)
        self.assertEqual(parsed["deepgram_api_key"], {"set": True, "source": "config"})

    def test_unset_secret_shows_none_source(self):
        code, out, _ = self.run_main(["config", "get", "--json"])
        parsed = json.loads(out)
        self.assertEqual(parsed["summary_api_key"], {"set": False, "source": "none"})

    def test_env_source_reported(self):
        with mock.patch.dict("os.environ", {"DEEPGRAM_API_KEY": "env-value"}):
            code, out, _ = self.run_main(["config", "get", "--json"])
        parsed = json.loads(out)
        self.assertEqual(parsed["deepgram_api_key"]["source"], "env")
        self.assertNotIn("env-value", out)

    def test_command_source_reported_without_running_it(self):
        self.config.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.config.CONFIG_FILE.write_text(json.dumps({"deepgram_api_key_command": "echo x"}))
        with mock.patch("spitball.config.subprocess.run") as run_mock:
            code, out, _ = self.run_main(["config", "get", "--json"])
        run_mock.assert_not_called()
        parsed = json.loads(out)
        self.assertEqual(parsed["deepgram_api_key"], {"set": True, "source": "command"})


class TestConfigSet(ConfigCliTestCase):
    def test_bool_and_number_parsed(self):
        code, _, _ = self.run_main(["config", "set", "summary_enabled", "false"])
        self.assertEqual(code, 0)
        code, _, _ = self.run_main(["config", "set", "min_manual_s", "5"])
        self.assertEqual(code, 0)
        raw = json.loads(self.config.CONFIG_FILE.read_text())
        self.assertIs(raw["summary_enabled"], False)
        self.assertEqual(raw["min_manual_s"], 5)
        self.assertIsInstance(raw["min_manual_s"], int)

    def test_plain_string_kept_as_string(self):
        code, _, _ = self.run_main(["config", "set", "my_name", "Morgan"])
        self.assertEqual(code, 0)
        raw = json.loads(self.config.CONFIG_FILE.read_text())
        self.assertEqual(raw["my_name"], "Morgan")

    def test_unknown_key_rejected(self):
        code, _, err = self.run_main(["config", "set", "not_a_real_key", "1"])
        self.assertEqual(code, 1)
        self.assertIn("unknown", err.lower())
        self.assertFalse(self.config.CONFIG_FILE.exists())

    def test_secret_key_rejected_use_set_secret_instead(self):
        code, _, err = self.run_main(["config", "set", "deepgram_api_key", "sneaky"])
        self.assertEqual(code, 1)
        self.assertIn("set-secret", err)
        self.assertFalse(self.config.CONFIG_FILE.exists())

    def test_file_becomes_mode_600(self):
        self.run_main(["config", "set", "my_name", "Morgan"])
        mode = self.config.CONFIG_FILE.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_preserves_unknown_and_untouched_keys(self):
        # Mirrors the live user config's shape: extras this version doesn't
        # manage as a first-class setting, plus ones it does.
        self.config.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.config.CONFIG_FILE.write_text(json.dumps({
            "deepgram_api_key_command": "op read op://x",
            "summary_base_url": "http://127.0.0.1:11434/v1",
            "summary_model": "llama3.2",
            "export_dir": "/home/morgan/notes",
            "my_name": "Morgan",
            "some_future_key_this_version_does_not_know_about": "keep-me",
        }))
        code, _, _ = self.run_main(["config", "set", "min_manual_s", "5"])
        self.assertEqual(code, 0)
        raw = json.loads(self.config.CONFIG_FILE.read_text())
        self.assertEqual(raw["deepgram_api_key_command"], "op read op://x")
        self.assertEqual(raw["summary_base_url"], "http://127.0.0.1:11434/v1")
        self.assertEqual(raw["summary_model"], "llama3.2")
        self.assertEqual(raw["export_dir"], "/home/morgan/notes")
        self.assertEqual(raw["my_name"], "Morgan")
        self.assertEqual(raw["some_future_key_this_version_does_not_know_about"], "keep-me")
        self.assertEqual(raw["min_manual_s"], 5)

    def test_sends_reload(self):
        with mock.patch("spitball.__main__.send") as send_mock:
            self.run_main(["config", "set", "my_name", "Morgan"])
        send_mock.assert_called_once_with("reload")

    def test_too_few_args_prints_usage(self):
        code, out, _ = self.run_main(["config", "set", "my_name"])
        self.assertEqual(code, 2)
        self.assertIn("usage", out)


class TestConfigSetSecret(ConfigCliTestCase):
    def test_reads_from_stdin_not_argv(self):
        code, _, _ = self.run_main(["config", "set-secret", "deepgram_api_key"], stdin_text="my-key-value\n")
        self.assertEqual(code, 0)
        raw = json.loads(self.config.CONFIG_FILE.read_text())
        self.assertEqual(raw["deepgram_api_key"], "my-key-value")

    def test_empty_stdin_clears_it(self):
        self.config.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.config.CONFIG_FILE.write_text(json.dumps({"deepgram_api_key": "old-value"}))
        code, _, _ = self.run_main(["config", "set-secret", "deepgram_api_key"], stdin_text="")
        self.assertEqual(code, 0)
        raw = json.loads(self.config.CONFIG_FILE.read_text())
        self.assertEqual(raw["deepgram_api_key"], "")

    def test_only_a_secret_key_allowed(self):
        code, _, err = self.run_main(["config", "set-secret", "my_name"], stdin_text="x")
        self.assertEqual(code, 1)
        self.assertIn("not a secret key", err)

    def test_never_printed_back(self):
        code, out, err = self.run_main(["config", "set-secret", "deepgram_api_key"], stdin_text="super-secret\n")
        self.assertNotIn("super-secret", out)
        self.assertNotIn("super-secret", err)

    def test_file_mode_600(self):
        self.run_main(["config", "set-secret", "deepgram_api_key"], stdin_text="x")
        mode = self.config.CONFIG_FILE.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)


class TestConfigUnset(ConfigCliTestCase):
    def test_removes_key_back_to_default(self):
        self.config.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.config.CONFIG_FILE.write_text(json.dumps({"my_name": "Morgan", "min_manual_s": 99}))
        code, _, _ = self.run_main(["config", "unset", "my_name"])
        self.assertEqual(code, 0)
        raw = json.loads(self.config.CONFIG_FILE.read_text())
        self.assertNotIn("my_name", raw)
        self.assertEqual(raw["min_manual_s"], 99)  # untouched

    def test_unset_secret_key_works_too(self):
        self.config.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.config.CONFIG_FILE.write_text(json.dumps({"deepgram_api_key": "x"}))
        code, _, _ = self.run_main(["config", "unset", "deepgram_api_key"])
        self.assertEqual(code, 0)
        raw = json.loads(self.config.CONFIG_FILE.read_text())
        self.assertNotIn("deepgram_api_key", raw)

    def test_unset_missing_key_is_a_noop_not_an_error(self):
        code, _, _ = self.run_main(["config", "unset", "my_name"])
        self.assertEqual(code, 0)

    def test_unknown_key_rejected(self):
        code, _, err = self.run_main(["config", "unset", "not_a_real_key"])
        self.assertEqual(code, 1)


class TestConfigReloadReachesDaemon(unittest.TestCase):
    """One end-to-end check (unlike the mocked `send` above) that `config
    set` really does wake a running daemon up to the new value -- same
    pattern as test_cli.py's CliTestCase."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp = Path(self.tmpdir.name)
        self._rt_cm = isolated_runtime(self.tmp)
        self.config = self._rt_cm.__enter__()
        self.addCleanup(self._rt_cm.__exit__, None, None, None)
        self._notify_patch = mock.patch("spitball.daemon.notify")
        self._notify_patch.start()
        self.addCleanup(self._notify_patch.stop)

    def test_config_set_triggers_daemon_reload(self):
        from spitball.daemon import Daemon
        d = Daemon()
        thread = threading.Thread(target=d.serve, daemon=True)
        thread.start()
        deadline = time.time() + 5
        while time.time() < deadline and not self.config.SOCKET_PATH.exists():
            time.sleep(0.02)

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
             mock.patch("sys.stdin", io.StringIO("")):
            code = cli.main(["config", "set", "my_name", "Morgan"])
        self.assertEqual(code, 0)

        deadline = time.time() + 5
        while time.time() < deadline and d.cfg.get("my_name") != "Morgan":
            time.sleep(0.02)
        self.assertEqual(d.cfg["my_name"], "Morgan")


class TestMicDenoiseKeys(ConfigCliTestCase):
    """docs/SPEC-v2.md section 3's two keys: plain settings (no secret), a
    string mode and an integer threshold, round-tripped through the CLI."""

    def test_defaults(self):
        code, out, _ = self.run_main(["config", "get", "--json"])
        self.assertEqual(code, 0)
        parsed = json.loads(out)
        self.assertEqual(parsed["mic_denoise"], "auto")
        self.assertEqual(parsed["mic_noise_floor_db"], -45)

    def test_set_and_unset_round_trip(self):
        self.assertEqual(self.run_main(["config", "set", "mic_denoise", "on"])[0], 0)
        self.assertEqual(self.run_main(["config", "set", "mic_noise_floor_db", "-52"])[0], 0)
        parsed = json.loads(self.run_main(["config", "get", "--json"])[1])
        self.assertEqual(parsed["mic_denoise"], "on")
        self.assertEqual(parsed["mic_noise_floor_db"], -52)
        self.assertIsInstance(parsed["mic_noise_floor_db"], int)
        self.assertEqual(self.run_main(["config", "unset", "mic_denoise"])[0], 0)
        parsed = json.loads(self.run_main(["config", "get", "--json"])[1])
        self.assertEqual(parsed["mic_denoise"], "auto")


if __name__ == "__main__":
    unittest.main()


class TestCalendarSecret(ConfigCliTestCase):
    def test_calendar_ics_url_is_a_secret(self):
        code, _, err = self.run_main(["config", "set", "calendar_ics_url", "https://x/y.ics"])
        self.assertEqual(code, 1)
        self.assertIn("set-secret", err)
        code, _, _ = self.run_main(["config", "set-secret", "calendar_ics_url"], stdin_text="https://x/private-abc/basic.ics\n")
        self.assertEqual(code, 0)
        code, out, _ = self.run_main(["config", "get", "--json"])
        data = json.loads(out)
        self.assertEqual(data["calendar_ics_url"], {"set": True, "source": "config"})
        self.assertNotIn("private-abc", out)
        self.assertEqual(stat.S_IMODE(self.config.CONFIG_FILE.stat().st_mode), 0o600)

    def test_calendar_keys_have_defaults_and_set_round_trips(self):
        code, out, _ = self.run_main(["config", "get", "--json"])
        data = json.loads(out)
        self.assertFalse(data["calendar_enabled"])
        self.assertEqual(data["calendar_source"], "ics")
        self.assertEqual(data["calendar_cache_ttl_s"], 900)
        self.assertTrue(data["calendar_prefer_event_title"])
        self.assertTrue(data["calendar_names_to_summary"])
        self.assertFalse(data["calendar_description_to_summary"])
        for key, raw, want in (("calendar_enabled", "true", True), ("calendar_source", "command", "command"),
                               ("calendar_command", "khal-json", "khal-json"), ("calendar_cache_ttl_s", "60", 60),
                               ("calendar_my_email", "me@example.com", "me@example.com")):
            self.assertEqual(self.run_main(["config", "set", key, raw])[0], 0)
            self.assertEqual(json.loads(self.run_main(["config", "get", "--json"])[1])[key], want)


class TestSpeakerKeys(ConfigCliTestCase):
    """docs/SPEC-v2.md section 4's three keys: two toggles and an integer
    cap, plain settings, round-tripped through the CLI."""

    def test_defaults(self):
        parsed = json.loads(self.run_main(["config", "get", "--json"])[1])
        self.assertTrue(parsed["speaker_names"])
        self.assertTrue(parsed["speaker_split"])
        self.assertEqual(parsed["speaker_max"], 6)

    def test_set_and_unset_round_trip(self):
        self.assertEqual(self.run_main(["config", "set", "speaker_names", "false"])[0], 0)
        self.assertEqual(self.run_main(["config", "set", "speaker_split", "false"])[0], 0)
        self.assertEqual(self.run_main(["config", "set", "speaker_max", "3"])[0], 0)
        parsed = json.loads(self.run_main(["config", "get", "--json"])[1])
        self.assertFalse(parsed["speaker_names"])
        self.assertFalse(parsed["speaker_split"])
        self.assertEqual(parsed["speaker_max"], 3)
        self.assertIsInstance(parsed["speaker_max"], int)
        self.assertEqual(self.run_main(["config", "unset", "speaker_max"])[0], 0)
        self.assertEqual(json.loads(self.run_main(["config", "get", "--json"])[1])["speaker_max"], 6)
