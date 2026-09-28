"""Proves the global safety net in tests/__init__.py is actually active:
config's module-level path defaults are session-temp paths (never the real
~/.config/spitball, ~/.local/state/spitball, or $XDG_RUNTIME_DIR/spitball),
and notify-send/xdg-open resolve to fakes that only log, never a real
notification or opener. This is a backstop for a background daemon thread
that outlives its own test's per-test isolation/mocks -- see tests/__init__.py
for the full story (a real notification escaped during this suite's own
development before this net existed)."""
import os
import shutil
import subprocess
import unittest
from pathlib import Path

import tests as tests_pkg
from spitball import config, daemon

REAL_CONFIG = Path.home() / ".config" / "spitball" / "config.json"
REAL_STATE_DIR = Path.home() / ".local" / "state" / "spitball"


class TestGlobalPathDefaultsAreIsolated(unittest.TestCase):
    def test_config_file_default_is_not_the_real_one(self):
        self.assertNotEqual(config.CONFIG_FILE, REAL_CONFIG)
        self.assertIn(str(tests_pkg.session_dir()), str(config.CONFIG_FILE))

    def test_state_dir_default_is_not_the_real_one(self):
        self.assertNotEqual(config.STATE_DIR, REAL_STATE_DIR)
        self.assertIn(str(tests_pkg.session_dir()), str(config.STATE_DIR))

    def test_runtime_dir_default_is_under_session_dir(self):
        self.assertIn(str(tests_pkg.session_dir()), str(config.RUNTIME_DIR))

    def test_default_config_load_never_reads_the_real_file(self):
        # Whatever the real file has (a real Deepgram key command, etc.),
        # config.load() with no override in play must never see it.
        cfg = config.load()
        self.assertEqual(cfg, config.DEFAULTS)


class TestFakeNotifyAndOpenAreOnPath(unittest.TestCase):
    def test_notify_send_resolves_to_the_fake(self):
        resolved = shutil.which("notify-send")
        self.assertIsNotNone(resolved)
        self.assertIn(str(tests_pkg.session_dir()), resolved)

    def test_xdg_open_resolves_to_the_fake(self):
        resolved = shutil.which("xdg-open")
        self.assertIsNotNone(resolved)
        self.assertIn(str(tests_pkg.session_dir()), resolved)

    def test_unmocked_daemon_notify_only_hits_the_fake(self):
        # Deliberately call the real daemon.notify() (not mocked) to prove
        # that even code that forgot to mock it can't reach a real notifier.
        log_before = tests_pkg.fake_bin_log().read_text() if tests_pkg.fake_bin_log().exists() else ""
        daemon.notify("isolation self-test", "body", "low")
        # notify() fires a detached Popen; give it a moment to actually run.
        import time
        for _ in range(50):
            log_after = tests_pkg.fake_bin_log().read_text() if tests_pkg.fake_bin_log().exists() else ""
            if len(log_after) > len(log_before):
                break
            time.sleep(0.05)
        else:
            log_after = tests_pkg.fake_bin_log().read_text() if tests_pkg.fake_bin_log().exists() else ""
        self.assertGreater(len(log_after), len(log_before))
        self.assertIn("notify-send", log_after[len(log_before):])


if __name__ == "__main__":
    unittest.main()
