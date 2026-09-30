"""Shared test helpers. Never touches the live service's runtime/state dirs,
~/Calls, ~/.config/spitball/config.json, or the real export dir -- every test
that needs paths gets its own tempfile.TemporaryDirectory() and points
config/cfg at it explicitly.
"""
from __future__ import annotations

import contextlib
import json
import threading
import time
from pathlib import Path
from unittest import mock

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def make_cfg(tmp: Path, **overrides) -> dict:
    """A full config dict pointed entirely at a temp dir -- safe to pass to
    process.process()/daemon.Daemon without touching the real ~/Calls, the
    real export_dir, or any live credentials."""
    from spitball import config
    cfg = dict(config.DEFAULTS)
    cfg["calls_dir"] = str(tmp / "Calls")
    cfg["export_dir"] = ""  # off by default; tests that want it set it explicitly
    cfg["detect_after_s"] = 0
    cfg["end_after_s"] = 1
    cfg["min_call_s"] = 2
    cfg["min_manual_s"] = 1
    # Off by default: most daemon/process tests aren't exercising live
    # transcription and mock Recording() so audio.opus never really exists --
    # leaving this on would spin up a real (if harmless) background
    # LiveTranscriber thread per recording. Tests that ARE about live
    # transcription (tests/test_live_transcriber.py, and the daemon-wiring
    # tests in tests/test_daemon.py) turn it on explicitly via `**overrides`.
    cfg["live_transcript"] = False
    cfg.update(overrides)
    return cfg


@contextlib.contextmanager
def isolated_runtime(tmp: Path):
    """Redirect config.RUNTIME_DIR/STATE_FILE/SOCKET_PATH/STATE_DIR/PERSIST_FILE/
    CONFIG_FILE at the module-attribute level for the duration of the `with`
    block, then restore them. Every consumer (`daemon.py`, `__main__.py`) looks
    these up as `config.ATTR` at call time, so patching the module object is
    sufficient -- no reload needed, and nothing ever reaches the real
    $XDG_RUNTIME_DIR/spitball, ~/.local/state/spitball, or ~/.config/spitball."""
    from spitball import config
    runtime_dir = tmp / "runtime"
    state_dir = tmp / "state"
    originals = {
        "RUNTIME_DIR": config.RUNTIME_DIR,
        "STATE_FILE": config.STATE_FILE,
        "SOCKET_PATH": config.SOCKET_PATH,
        "STATE_DIR": config.STATE_DIR,
        "PERSIST_FILE": config.PERSIST_FILE,
        "CONFIG_FILE": config.CONFIG_FILE,
    }
    config.RUNTIME_DIR = runtime_dir
    config.STATE_FILE = runtime_dir / "state.json"
    config.SOCKET_PATH = runtime_dir / "ctl.sock"
    config.STATE_DIR = state_dir
    config.PERSIST_FILE = state_dir / "persist.json"
    # Point at a config.json that can never exist, so config.load() always
    # returns pure DEFAULTS regardless of what's in the real user's config.
    config.CONFIG_FILE = tmp / "no-such-config" / "config.json"
    try:
        yield config
    finally:
        for k, v in originals.items():
            setattr(config, k, v)


def new_daemon(tmp: Path, **cfg_overrides):
    """Construct a Daemon() with runtime/state fully isolated under `tmp` and
    its cfg pointed at a temp calls_dir. Caller is expected to already be
    inside an `isolated_runtime(tmp)` block."""
    from spitball import providers
    from spitball.daemon import Daemon
    d = Daemon()
    d.cfg = make_cfg(tmp, **cfg_overrides)
    # Daemon.__init__ already computed setup_needed from the (isolated,
    # pure-DEFAULTS) cfg it loaded before this override -- recompute it
    # against the actual test cfg so it's not stale/misleading.
    d.setup_needed = providers.setup_needed(d.cfg)
    return d


def pactl_source_outputs(streams: list) -> str:
    return json.dumps(streams)


def pactl_sources_short(sources: list) -> str:
    return json.dumps(sources)


class TrackedThread(threading.Thread):
    """A real threading.Thread that also registers itself in a shared list, so
    a test can find and .join() the background thread a daemon method spawned
    without racing it. Never mocks the thread itself -- it still runs for
    real, just observably."""
    registry: list | None = None

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        if TrackedThread.registry is not None:
            TrackedThread.registry.append(self)


@contextlib.contextmanager
def track_threads():
    """Patches spitball.daemon.threading.Thread so every thread the Daemon
    spawns (e.g. _process()) lands in the returned list, in creation order."""
    threads: list = []
    TrackedThread.registry = threads
    with mock.patch("spitball.daemon.threading.Thread", TrackedThread):
        try:
            yield threads
        finally:
            TrackedThread.registry = None


class Gate:
    """Lets a test pause a mocked call inside a background thread: the mock's
    side_effect enters the gate (signalling `entered`) and blocks on `release`
    until the test lets it continue. Used to make daemon processing
    deterministically observable mid-flight without ever sleeping and hoping."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def wait_then(self, fn):
        def _inner(*a, **kw):
            self.entered.set()
            self.release.wait(10)
            return fn(*a, **kw)
        return _inner


class FakeClock:
    """A controllable stand-in for time.time(), for deterministic detect_after_s
    / end_after_s transition tests. Patch with
    mock.patch("spitball.daemon.time.time", side_effect=clock)."""

    def __init__(self, start: float = 1_700_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, dt: float):
        self.now += dt


def make_fake_recording(alive: bool = True, stop_duration: float = 100.0, started_at: float = None):
    """A Mock standing in for a recorder.Recording instance -- never spawns a
    real ffmpeg. Assign to `mock.patch("spitball.daemon.Recording").return_value`."""
    rec = mock.Mock()
    rec.started_at = started_at if started_at is not None else time.time()
    rec.alive.return_value = alive
    rec.stop.return_value = stop_duration
    return rec
