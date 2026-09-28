import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spitball import config


class TestSecret(unittest.TestCase):
    def setUp(self):
        # Never let a real env var leak into the resolution-order tests.
        self._env_patch = mock.patch.dict(os.environ, {}, clear=False)
        self._env_patch.start()
        os.environ.pop("SPITBALL_TEST_KEY", None)
        self.addCleanup(self._env_patch.stop)

    def test_env_var_wins_over_everything(self):
        cfg = {"my_key": "cfg-value", "my_key_command": "echo cmd-value"}
        with mock.patch.dict(os.environ, {"SPITBALL_TEST_KEY": "  env-value  "}):
            self.assertEqual(config.secret(cfg, "my_key", env="SPITBALL_TEST_KEY"), "env-value")

    def test_config_value_wins_over_command(self):
        cfg = {"my_key": " cfg-value ", "my_key_command": "echo cmd-value"}
        self.assertEqual(config.secret(cfg, "my_key", env="SPITBALL_TEST_KEY"), "cfg-value")

    def test_command_runs_when_no_value(self):
        cfg = {"my_key": "", "my_key_command": "echo  cmd-value  "}
        self.assertEqual(config.secret(cfg, "my_key"), "cmd-value")

    def test_command_failure_returns_empty(self):
        cfg = {"my_key": "", "my_key_command": "exit 1"}
        self.assertEqual(config.secret(cfg, "my_key"), "")

    def test_command_empty_stdout_returns_empty(self):
        cfg = {"my_key": "", "my_key_command": "true"}
        self.assertEqual(config.secret(cfg, "my_key"), "")

    def test_no_env_no_value_no_command_returns_empty(self):
        cfg = {"my_key": "", "my_key_command": ""}
        self.assertEqual(config.secret(cfg, "my_key", env="SPITBALL_TEST_KEY"), "")

    def test_no_env_arg_skips_env_lookup(self):
        cfg = {"my_key": "cfg-value"}
        with mock.patch.dict(os.environ, {"SOME_OTHER_KEY": "x"}):
            self.assertEqual(config.secret(cfg, "my_key"), "cfg-value")

    def test_command_uses_shell_and_is_capped_by_timeout(self):
        cfg = {"my_key": "", "my_key_command": "printf key"}
        with mock.patch("spitball.config.subprocess.run",
                         wraps=subprocess.run) as run_mock:
            result = config.secret(cfg, "my_key")
        self.assertEqual(result, "key")
        _, kwargs = run_mock.call_args
        self.assertEqual(kwargs.get("timeout"), 30)
        self.assertTrue(kwargs.get("shell"))


class TestFirstName(unittest.TestCase):
    def _pw(self, gecos):
        pw = mock.Mock()
        pw.pw_gecos = gecos
        return pw

    def test_uses_first_token_of_gecos(self):
        with mock.patch("spitball.config.pwd.getpwuid", return_value=self._pw("Morgan Lee,,,,")):
            self.assertEqual(config._first_name(), "Morgan")

    def test_empty_gecos_falls_back_to_me(self):
        with mock.patch("spitball.config.pwd.getpwuid", return_value=self._pw("")):
            self.assertEqual(config._first_name(), "Me")

    def test_lookup_failure_falls_back_to_me(self):
        with mock.patch("spitball.config.pwd.getpwuid", side_effect=KeyError("no such uid")):
            self.assertEqual(config._first_name(), "Me")

    def test_single_name_gecos(self):
        with mock.patch("spitball.config.pwd.getpwuid", return_value=self._pw("Morgan")):
            self.assertEqual(config._first_name(), "Morgan")


class TestAtomicWrite(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_creates_parent_dirs(self):
        path = self.dir / "a" / "b" / "state.json"
        config.atomic_write(path, "hello")
        self.assertEqual(path.read_text(), "hello")

    def test_no_tmp_file_left_behind(self):
        path = self.dir / "state.json"
        config.atomic_write(path, "content")
        leftovers = [p for p in self.dir.iterdir() if p.name != "state.json"]
        self.assertEqual(leftovers, [])

    def test_mode_applied(self):
        path = self.dir / "state.json"
        config.atomic_write(path, "content", mode=0o600)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_overwrite_replaces_content(self):
        path = self.dir / "state.json"
        config.atomic_write(path, "first")
        config.atomic_write(path, "second")
        self.assertEqual(path.read_text(), "second")


class TestLoad(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_file = Path(self.tmp.name) / "config.json"
        self._patch = mock.patch.object(config, "CONFIG_FILE", self.config_file)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_missing_file_returns_defaults(self):
        cfg = config.load()
        self.assertEqual(cfg, config.DEFAULTS)

    def test_overrides_merge_over_defaults(self):
        self.config_file.write_text(json.dumps({"my_name": "Morgan", "min_call_s": 5}))
        cfg = config.load()
        self.assertEqual(cfg["my_name"], "Morgan")
        self.assertEqual(cfg["min_call_s"], 5)
        self.assertEqual(cfg["deepgram_model"], config.DEFAULTS["deepgram_model"])

    def test_invalid_json_falls_back_to_defaults(self):
        self.config_file.write_text("{not valid json")
        cfg = config.load()
        self.assertEqual(cfg, config.DEFAULTS)


if __name__ == "__main__":
    unittest.main()
