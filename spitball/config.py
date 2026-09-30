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
    # Start recording automatically the moment a call is detected. Used to live
    # in ~/.local/state/spitball/persist.json; migrated into config.json once
    # (see daemon.Daemon._migrate_auto_record) so the settings panel can edit it
    # like any other setting.
    "auto_record": False,
    # Spoken language: "en" (default), "auto" (let the provider detect it --
    # deepgram uses its own language-detection mode; local just follows
    # whatever voxtype's active model supports), or an ISO code.
    "language": "en",
    # Which transcription provider to use: "local" (default, on-device via
    # voxtype -- audio never leaves the machine) or "deepgram". See
    # spitball/providers/. (OpenAI-compatible/AssemblyAI/Soniox are phase 2 --
    # see docs/ROADMAP.md.) Spitball never manages voxtype's own model/engine
    # choice -- "local" just runs whatever the user already has voxtype
    # configured with.
    "transcription_provider": "local",
    # Deepgram key: first match wins among the DEEPGRAM_API_KEY env var,
    # "deepgram_api_key", and "deepgram_api_key_command" (run it, use stdout; handy
    # for password managers, e.g. "op read op://Private/Deepgram/credential").
    "deepgram_api_key": "",
    "deepgram_api_key_command": "",
    "deepgram_model": "nova-3",
    # Summaries: any OpenAI-compatible chat endpoint. The default is a local Ollama.
    # If it can't be reached you still get audio + transcript, and
    # `spitball reprocess <dir>` fills in the summary later.
    "summary_base_url": "http://127.0.0.1:11434/v1",
    "summary_model": "",          # empty = first model the endpoint lists
    "summary_api_key": "",
    "summary_api_key_command": "",
    "summary_enabled": True,
    "call_apps": DEFAULT_CALL_APPS,
    # Live transcript while recording (spitball/live.py): near-real-time,
    # local-provider-only (never deepgram, regardless of
    # transcription_provider -- it never costs money or sends audio
    # anywhere), shown in the bar's Live popup. Runs only while voxtype is
    # actually available; see providers.local.ready().
    "live_transcript": True,
    # A still-open (no pause yet) live segment is cut here regardless, so one
    # long uninterrupted stretch of talk doesn't grow forever before the
    # first line appears.
    "live_max_window_s": 12,
    # Use the persistent live engine (spitball/live_engine.py) when it's set
    # up -- a kept-loaded Parakeet model instead of a fresh `voxtype
    # transcribe` per segment. false forces the voxtype path.
    "live_engine": True,
    # A mic stream must exist this long before we call it a detected call.
    "detect_after_s": 2,
    # The app must let go of the mic this long before we call the call over.
    "end_after_s": 8,
    # Throw away recordings shorter than this (auto-detected / manual starts).
    "min_call_s": 60,
    "min_manual_s": 10,
    "opus_bitrate": "32k",
    # Mic noise reduction for the transcriber (spitball/denoise.py,
    # docs/SPEC-v2.md §3): "off", "auto", or "on". Applied to a temporary copy
    # of the mic channel only -- the far channel and the recording itself are
    # never touched. "auto" measures the mic's background level and denoises
    # only when it is above mic_noise_floor_db (dBFS): a quiet headset call
    # stays as recorded, a fan or a cafe gets RNNoise (ffmpeg arnndn, model in
    # models/rnnoise/, afftdn as the fallback). Which mode actually ran is
    # recorded in .transcript.json / .live.json.
    "mic_denoise": "auto",
    "mic_noise_floor_db": -45,
    # Calendar (spitball/calendar.py, docs/SPEC-v2.md §2): match each call to
    # the meeting it belongs to, then name the folder after it, head the
    # transcript with the meeting + attendees, and tell the summarizer who
    # was invited. Off by default; a calendar failure never blocks recording.
    "calendar_enabled": False,
    # "ics": a secret iCal/ICS/webcal address (Google Calendar -> Settings ->
    # the calendar -> "Secret address in iCal format"), fetched and cached.
    # "command": run `calendar_command` and read normalized JSON events from
    # its stdout (shape in CONTRACT.md).
    "calendar_source": "ics",
    # The feed address IS a credential (anyone holding it can read the
    # calendar until it is reset), so it's a secret: set-secret only, masked
    # in `config get`, never logged. `_command` is the password-manager route.
    "calendar_ics_url": "",
    "calendar_ics_url_command": "",
    "calendar_command": "",
    # Re-download the feed once it's older than this (seconds). The whole
    # calendar arrives on every fetch (a long-lived calendar is several MB),
    # so a call never waits on it twice in a row.
    "calendar_cache_ttl_s": 900,
    # On a confident match, the event title becomes the call's title (folder
    # slug, transcript heading, summary heading). Off keeps the summarizer's
    # own title and adds only the meeting header. A weak match never renames.
    "calendar_prefer_event_title": True,
    # Attendee names go to the summarizer (wherever summary_base_url points --
    # local by default) so it can attribute statements; the description goes
    # only when explicitly turned on, since it can carry private text.
    "calendar_names_to_summary": True,
    "calendar_description_to_summary": False,
    # Your own address on the calendar, to read your response (declined
    # invites are skipped). Empty = detect it: the address on nearly every
    # invite in the feed is the owner's.
    "calendar_my_email": "",
}

# The secrets the settings panel/CLI ever handles. Never settable via
# `config set` (that would put them on argv/in shell history) -- only via
# `config set-secret`, which reads the value from stdin. (Grows again in
# phase 2 as more transcription providers add their own key.)
SECRET_KEYS = ("deepgram_api_key", "summary_api_key", "calendar_ics_url")

# Secret key -> the environment variable that overrides it (empty = none).
SECRET_ENV = {
    "deepgram_api_key": "DEEPGRAM_API_KEY",
    "summary_api_key": "",
    "calendar_ics_url": "",
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


def secret_status(cfg: dict, key: str) -> dict:
    """{"set": bool, "source": "env"|"config"|"command"|"none"} for one of the
    SECRET_KEYS -- never resolves the value itself (no subprocess, no
    reading the actual secret), so `config get --json` stays instant and
    can't leak anything. This is what `config get --json` masks each
    *_api_key down to."""
    env = SECRET_ENV.get(key, "")
    if env and os.environ.get(env):
        return {"set": True, "source": "env"}
    if cfg.get(key):
        return {"set": True, "source": "config"}
    if cfg.get(f"{key}_command"):
        return {"set": True, "source": "command"}
    return {"set": False, "source": "none"}


def read_raw() -> dict:
    """The user's config.json exactly as written -- no DEFAULTS merged in.
    Used by the config-editing CLI commands (set/unset/set-secret) so a write
    only ever touches the one key it means to, preserving every other key
    already in the file (including ones this version of Spitball doesn't
    know about)."""
    try:
        data = json.loads(CONFIG_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_raw(data: dict) -> None:
    """Writes the raw config dict back atomically, mode 600 -- once a config
    file holds any key at all it may hold a secret, so every write from here
    on keeps it private rather than trying to track exactly which key made it
    sensitive."""
    atomic_write(CONFIG_FILE, json.dumps(data, indent=1) + "\n", mode=0o600)


def set_key(key: str, value) -> None:
    """Reads the raw file, sets one key, writes it back -- preserving every
    other key. Used both by `config set`/`set-secret` and by the daemon's
    one-time auto_record migration."""
    raw = read_raw()
    raw[key] = value
    write_raw(raw)


def unset_key(key: str) -> None:
    raw = read_raw()
    raw.pop(key, None)
    write_raw(raw)


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
