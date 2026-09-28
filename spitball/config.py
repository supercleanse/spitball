"""Paths and settings for Spitball.

Settings live in ~/.config/spitball/config.json (optional; every key has a default).
Runtime state (the file the bar widget reads, the control socket) lives under
$XDG_RUNTIME_DIR/spitball/. Durable bits (auto-record flag, last call) live under
~/.local/state/spitball/.
"""
from __future__ import annotations

import json
import os
import pwd
import subprocess
import threading
from pathlib import Path

PROGRAM_ROOT = Path(__file__).resolve().parents[1]

# Every path below defaults to the real, production location. Tests (and any
# throwaway second instance) can redirect them with these env vars so they
# never touch the live service's runtime dir, state dir, or config file.
RUNTIME_DIR = Path(os.environ.get("SPITBALL_RUNTIME_DIR") or
                    (Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "spitball"))
STATE_FILE = RUNTIME_DIR / "state.json"
SOCKET_PATH = RUNTIME_DIR / "ctl.sock"
STATE_DIR = Path(os.environ.get("SPITBALL_STATE_DIR") or (Path.home() / ".local" / "state" / "spitball"))
PERSIST_FILE = STATE_DIR / "persist.json"
CONFIG_FILE = Path(os.environ.get("SPITBALL_CONFIG") or (Path.home() / ".config" / "spitball" / "config.json"))

# Mic users that count as "a call". Matched case-insensitively against the stream's
# application.process.binary and application.name. Value = display name in the bar.
DEFAULT_CALL_APPS = {
    "zoom": "Zoom",
    "chromium": "Chromium",
    "chrome": "Chrome",
    "brave": "Brave",
    "firefox": "Firefox",
    "slack": "Slack",
    "teams": "Teams",
    "discord": "Discord",
    "signal": "Signal",
    "webex": "Webex",
    "whatsapp": "WhatsApp",
}

def _first_name() -> str:
    """The account's first name from its GECOS field, for labeling your side."""
    try:
        gecos = pwd.getpwuid(os.getuid()).pw_gecos.split(",")[0].strip()
        return gecos.split()[0] if gecos else "Me"
    except (KeyError, IndexError):
        return "Me"


DEFAULTS = {
    "calls_dir": str(Path.home() / "Calls"),
    # Optional: also copy each call's summary + transcript (never the audio) as one
    # markdown file into this folder, e.g. a notes vault. Empty = off.
    "export_dir": "",
    "my_name": _first_name(),
    # Deepgram key: first match wins among the DEEPGRAM_API_KEY env var,
    # "deepgram_api_key", and "deepgram_api_key_command" (run it, use stdout; handy
    # for password managers, e.g. "op read op://Private/Deepgram/credential").
    "deepgram_api_key": "",
    "deepgram_api_key_command": "",
    # Summaries: any OpenAI-compatible chat endpoint. The default is a local Ollama.
    # If it can't be reached you still get audio + transcript, and
    # `spitball reprocess <dir>` fills in the summary later.
    "summary_base_url": "http://127.0.0.1:11434/v1",
    "summary_model": "",          # empty = first model the endpoint lists
    "summary_api_key": "",
    "summary_api_key_command": "",
    "summary_enabled": True,
    "call_apps": DEFAULT_CALL_APPS,
    # A mic stream must exist this long before we call it a detected call.
    "detect_after_s": 2,
    # The app must let go of the mic this long before we call the call over.
    "end_after_s": 8,
    # Throw away recordings shorter than this (auto-detected / manual starts).
    "min_call_s": 60,
    "min_manual_s": 10,
    "deepgram_model": "nova-3",
    "opus_bitrate": "32k",
}


def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text()))
    except (OSError, ValueError):
        pass
    return cfg


def secret(cfg: dict, key: str, env: str = "") -> str:
    """Resolve a credential from env, then the config value, then its _command."""
    if env and os.environ.get(env):
        return os.environ[env].strip()
    if cfg.get(key):
        return str(cfg[key]).strip()
    cmd = cfg.get(f"{key}_command")
    if cmd:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    return ""


def atomic_write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique per thread, not just per process: two threads in the same daemon
    # (e.g. a background call finishing up while the main loop shuts down) can
    # both publish the state file around the same time, and a pid-only temp
    # name would collide -- the second os.replace() would find its tmp file
    # already moved away by the first.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)
