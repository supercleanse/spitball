"""Global, session-wide safety net -- runs before any `spitball.*` module is
ever imported anywhere in the test suite (Python always initializes a parent
package's __init__.py before any of its submodules, and every test file is
reached as tests.test_x or tests.live.test_x, so this always runs first).

Two independent layers, because per-test mocking alone was proven NOT
sufficient: a background daemon thread spawned by stop()/tick() can outlive
the test method that spawned it (its mocks and isolated_runtime() context
restore synchronously when the test method returns, but the thread keeps
running), and a straggler that wins that race would otherwise hit the REAL
config path and fire a REAL notify-send. This happened once during this
suite's own development (see Brain/CHANGELOG.md / the test report) --
confirmed by a live desktop notification that traced back to a test's
missing Deepgram key, not the real daemon.

1. The SPITBALL_RUNTIME_DIR / SPITBALL_STATE_DIR / SPITBALL_CONFIG env vars
   are set here, before spitball.config computes its module-level path
   constants from them. So even a stray, un-joined background thread that
   outlives its test's own isolated_runtime() override falls back to this
   session-wide temp dir, never $XDG_RUNTIME_DIR/spitball, ~/.local/state,
   or ~/.config/spitball.
2. A fake `notify-send` and `xdg-open` go on PATH ahead of the real ones, so
   even code that forgot to mock spitball.daemon.notify()/__main__._open()
   can never pop a real desktop notification or launch a real opener.
   Every (fake) invocation is appended to fake_bin_log() for tests that want
   to assert on it.

Individual tests still use isolated_runtime()/mock.patch for their own
assertions and for realistic per-test paths -- this is strictly a backstop,
not a replacement.
"""
from __future__ import annotations

import atexit
import os
import shutil
import stat
import tempfile
from pathlib import Path

_SESSION_DIR = Path(tempfile.mkdtemp(prefix="spitball-tests-session-"))
atexit.register(shutil.rmtree, str(_SESSION_DIR), True)

os.environ["SPITBALL_RUNTIME_DIR"] = str(_SESSION_DIR / "runtime")
os.environ["SPITBALL_STATE_DIR"] = str(_SESSION_DIR / "state")
# A config.json that can never exist, exactly like isolated_runtime()'s
# per-test override: config.load() always returns pure DEFAULTS here.
os.environ["SPITBALL_CONFIG"] = str(_SESSION_DIR / "no-such-config" / "config.json")

_FAKE_BIN = _SESSION_DIR / "fake-bin"
_FAKE_BIN.mkdir(parents=True, exist_ok=True)
_FAKE_LOG = _SESSION_DIR / "fake-bin-calls.log"


def _install_fake(name: str) -> None:
    script = _FAKE_BIN / name
    script.write_text(
        "#!/bin/sh\n"
        f'printf \'%s %s %s\\n\' "$(date -Iseconds)" "{name}" "$*" >> "{_FAKE_LOG}"\n'
        "exit 0\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


_install_fake("notify-send")
_install_fake("xdg-open")
os.environ["PATH"] = str(_FAKE_BIN) + os.pathsep + os.environ.get("PATH", "")


def session_dir() -> Path:
    return _SESSION_DIR


def fake_bin_log() -> Path:
    """Every (fake) notify-send/xdg-open invocation, one line per call. Used
    by tests/test_isolation.py to prove the backstop actually intercepts
    calls, and available to any test that wants to assert nothing fired."""
    return _FAKE_LOG
