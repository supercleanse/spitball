"""Local (on-device) transcription via voxtype, which ships with Omarchy.
Audio never leaves the machine.

Spitball does not manage voxtype's transcription itself -- it runs plain
`voxtype -q transcribe <file>` with no `--model`/`--engine` override, so
transcription follows whatever the user already has voxtype configured with
(Whisper base.en out of the box on Omarchy, or Parakeet/anything else they
picked). Spitball never touches `~/.config/voxtype/config.toml` or downloads
a model on its own path.

The one exception is `spitball local set-model <name>`, a convenience for
*switching* what voxtype is configured with. It never blocks the caller: it
starts the switch and returns immediately (see `set_model()`), reporting
progress to `$XDG_RUNTIME_DIR/spitball/model.json` (CONTRACT.md) so the
settings popup (when reopened) and the bar widget (while it's running) can
both show it without polling a CLI command.

The switch itself happens in a detached child process
(`run_set_model_worker()`, re-invoked as `spitball local _set-model-worker
<name>` -- an internal command, not for interactive use) so it survives the
short-lived CLI call that kicked it off:

1. If the model's engine (whisper/parakeet) differs from voxtype's current
   one, `pkexec voxtype setup onnx --enable/--disable` -- this pops Omarchy's
   own graphical polkit dialog (`/usr/share/omarchy/shell/plugins/polkit/`),
   never a terminal prompt. A canceled/denied prompt is reported as its own
   error, not a generic failure.
2. `voxtype setup --download --model <name> --activate --progress-format
   json`, whose NDJSON events on stdout
   (`{"event":"progress","bytes":N,"total":N,...}` /
   `{"event":"error","message":"..."}` / `{"event":"done",...}` -- verified
   against a real `voxtype setup --download --model base.en
   --progress-format json` run, which is instant since that model ships
   installed) are translated straight into model.json.
3. For a parakeet model, `apply_streaming_config()`: `voxtype config set
   parakeet.streaming true|false` -- on for a streaming-capable model
   (STREAMING_MODELS), off otherwise. Turning it on also writes the three
   streaming window sizes into voxtype's [parakeet] table if they're
   missing, since `voxtype config set` can't and voxtype's defaults fail
   its own validation -- the one place Spitball edits voxtype's config file
   itself.
4. `systemctl --user restart voxtype`, only if that unit is currently active.

Fallback: if `pkexec` isn't on PATH, or Omarchy's own shell (and therefore
its embedded polkit agent -- it's a `service`-kind plugin living inside the
omarchy-shell process, not a separate daemon) doesn't answer `omarchy-shell
shell ping`, there is no way to show a graphical password prompt at all --
`set_model()` falls back to the original interactive approach instead: a
floating terminal running `bin/spitball-upgrade-parakeet` (kept, and already
model/engine-agnostic despite its name), with a real sudo prompt the user
can see and type into. model.json is written to say so (`state: "terminal"`)
so both UIs stop expecting further progress from it.

Spitball's own long-lived processes (the daemon, the CLI invocation that
calls `set_model()`) never run `sudo` or `pkexec` themselves, and never touch
voxtype's config directly -- only the detached worker or the fallback script
do, and only via voxtype's own commands (plus the streaming window sizes
described in step 3).

voxtype has no timestamps of its own, so each channel is segmented into
speech windows ourselves (ffmpeg silencedetect) and each window is
transcribed as its own utterance.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .. import audio as audio_mod
from .. import config
from .. import denoise

NOT_INSTALLED_MESSAGE = "Install dictation (voxtype) or pick a cloud service"
VOXTYPE_LIB_DIR = Path("/usr/lib/voxtype")
MODEL_DIR = Path.home() / ".local" / "share" / "voxtype" / "models"  # read-only: sizing installed models
SET_MODEL_SCRIPT = config.PROGRAM_ROOT / "bin" / "spitball-upgrade-parakeet"
SPITBALL_CLI = config.PROGRAM_ROOT / "bin" / "spitball"
MODEL_STATE_FILENAME = "model.json"

# Valid model.json `state` values, in the order a switch normally moves
# through (skipping "switching-engine" when the engine is already right).
# "terminal" and "error" are the two ways a switch stops short of "done".
MODEL_SWITCH_STATES = ("switching-engine", "downloading", "activating", "restarting",
                        "done", "error", "terminal")

RECOMMENDED_MODEL = "parakeet-unified-en-0.6b"
# Parakeet models that support voxtype's cache-aware streaming (text typed
# while you're still talking). Switching to one of these turns
# `parakeet.streaming` on; switching to any other parakeet model turns it off,
# since voxtype refuses to load a non-streaming model with streaming on.
STREAMING_MODELS = ("parakeet-unified-en-0.6b",)
# voxtype's built-in streaming window defaults fail its own validation
# ("left_context_secs must map to a mel-frame count divisible by 8"), and
# `voxtype config set` doesn't expose these keys, so streaming only works once
# they're written into [parakeet] by hand. Each is a multiple of 0.08s (8 mel
# frames at 10ms); these are NVIDIA's usual cache-aware FastConformer sizes.
STREAMING_WINDOWS = (
    ("streaming_chunk_secs", "1.12"),
    ("streaming_left_context_secs", "5.6"),
    ("streaming_right_context_secs", "1.04"),
)
_ONLY_ENGINES = ("whisper", "parakeet")

# Approximate sizes for models NOT currently installed (an installed model's
# size is measured from disk instead -- see _installed_size_mb). Whisper
# figures are ggerganov/whisper.cpp's long-published ggml sizes; the
# parakeet ones are estimates (int8 quantization roughly quarters an fp32
# ONNX export) except v3-int8 and unified-en, which match what we measured on
# this machine after downloading them for real.
KNOWN_SIZE_MB = {
    "tiny": 75, "tiny.en": 75, "base": 148, "base.en": 148,
    "small": 466, "small.en": 466, "medium": 1533, "medium.en": 1533,
    "large-v3": 3094, "large-v3-turbo": 1624,
    "parakeet-tdt-0.6b-v2": 2450, "parakeet-tdt-0.6b-v2-int8": 639,
    "parakeet-tdt-0.6b-v3": 2450, "parakeet-tdt-0.6b-v3-int8": 639,
    "parakeet-unified-en-0.6b": 2399,
}

_HEADER_PREFIXES = ("Loading audio file:", "Audio format:", "Processing ")
# voxtype's log lines (e.g. an ONNX runtime error) can land on stdout too:
# "\x1b[2m2026-09-28T16:07:56Z\x1b[0m \x1b[31mERROR\x1b[0m …" or the same uncolored.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_LOG_LINE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\S+\s+(TRACE|DEBUG|INFO|WARN|ERROR)\b")
MIN_RETRY_S = 4.0  # stop halving a failing window below this length


def _clean_stdout(stdout: str) -> str:
    """Strips voxtype's non-text header lines and any log lines, leaving only
    the transcript text."""
    lines = []
    for raw in stdout.splitlines():
        line = _ANSI_RE.sub("", raw).strip()
        if not line or line.startswith(_HEADER_PREFIXES) or _LOG_LINE_RE.match(line):
            continue
        lines.append(line)
    return " ".join(lines).strip()


def _failed(result) -> bool:
    """A nonzero exit, or an ERROR log line on stdout (voxtype has exited 0
    with an ONNX runtime error printed where the text should be)."""
    if result.returncode != 0:
        return True
    for raw in (result.stdout or "").splitlines():
        m = _LOG_LINE_RE.match(_ANSI_RE.sub("", raw).strip())
        if m and m.group(1) == "ERROR":
            return True
    return False


def _active_engine_is_onnx() -> bool:
    """True if the currently *running* voxtype binary is an ONNX build
    (i.e. Parakeet-capable), per `voxtype setup onnx --status`'s "Active
    engine:" line."""
    try:
        out = subprocess.run(["voxtype", "setup", "onnx", "--status"],
                              capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    # Whisper build: "Active engine: Whisper" / "Binary: …/voxtype-avx2".
    # ONNX build:    "Active engine: Parakeet" / "Backend: ONNX (AVX2)".
    for line in out.splitlines():
        key, _, value = line.strip().partition(":")
        if key in ("Active engine", "Backend", "Binary") and "onnx" in value.lower():
            return True
    return False


def _onnx_binaries_present() -> bool:
    return VOXTYPE_LIB_DIR.is_dir() and any(VOXTYPE_LIB_DIR.glob("voxtype-onnx-*"))


def info() -> dict:
    """{"installed", "engine", "model", "onnx", "can_upgrade_parakeet",
    "message"} -- engine/model read from voxtype's own resolved config
    (`voxtype config get`), never a Spitball-managed setting. Used by
    `spitball local info --json` and the Settings panel's Local section."""
    if not shutil.which("voxtype"):
        return {"installed": False, "engine": "", "model": "", "onnx": False,
                "can_upgrade_parakeet": False, "message": NOT_INSTALLED_MESSAGE}
    try:
        engine = subprocess.run(["voxtype", "config", "get", "engine"],
                                 capture_output=True, text=True, timeout=10).stdout.strip()
        model = ""
        if engine:
            model = subprocess.run(["voxtype", "config", "get", f"{engine}.model"],
                                    capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        engine, model = "", ""
    onnx = _active_engine_is_onnx()
    can_upgrade = engine != "parakeet" and _onnx_binaries_present()
    label = engine.capitalize() if engine else "voxtype"
    message = f"Using voxtype: {label} {model}".strip() if model else f"Using voxtype: {label}"
    return {"installed": True, "engine": engine, "model": model, "onnx": onnx,
            "can_upgrade_parakeet": can_upgrade, "message": message}


# --------------------------------------------------------------- transcribe

def _transcribe_channel(wav: Path, channel: int, tmp: Path, vad: bool = False) -> list:
    """Segments one channel into speech windows and transcribes each.

    `vad=True` is the Whisper path's tighter speech detection (docs/SPEC-v2.md
    section 3): Whisper hallucinates text over audio with no speech in it, so
    (1) when steady background noise sits above `silencedetect`'s fixed
    -35 dB gate -- which otherwise leaves the channel with no detectable
    pauses and cuts it into 30 s blocks of noise -- the gate is raised to
    10 dB above the measured floor, and (2) any window whose loudest 50 ms
    frame stays within 6 dB of the floor is skipped as noise-only. Both come
    from one cheap `astats` pass (audio.measure_levels), no extra tools.
    Parakeet is far less prone to hallucinating, so it keeps the plain
    -35 dB segmentation (`vad=False`)."""
    duration = audio_mod.ffprobe_duration(wav)
    levels: list[float] = []
    floor = None
    noise_db = denoise.SILENCE_GATE_DB
    if vad:
        levels = audio_mod.measure_levels(wav)
        floor = denoise.noise_floor_db(levels)
        noise_db = denoise.adaptive_silence_db(floor)
    silences = audio_mod.detect_silence(wav, noise_db=noise_db)
    windows = audio_mod.speech_windows(duration, silences)
    utterances = []
    for i, (start, end) in enumerate(windows):
        if end - start < 0.4:
            continue
        if vad and not denoise.has_speech(levels, start, end, floor):
            continue
        utterances += _transcribe_window(wav, channel, start, end, tmp, f"ch{channel}-w{i}")
    return utterances


def _transcribe_window(wav: Path, channel: int, start: float, end: float, tmp: Path,
                       tag: str) -> list:
    """One voxtype run on [start, end). If voxtype fails, retry the two halves,
    down to MIN_RETRY_S; a piece that still fails becomes a visible gap marker
    instead of silently disappearing."""
    clip = tmp / f"{tag}.wav"
    audio_mod.extract_clip(wav, start, end, clip)
    try:
        r = subprocess.run(["voxtype", "-q", "transcribe", str(clip)],
                           capture_output=True, text=True, timeout=300)
        ok = not _failed(r)
    except subprocess.TimeoutExpired:
        r, ok = None, False
    finally:
        clip.unlink(missing_ok=True)
    if ok:
        text = _clean_stdout(r.stdout)
        return [{"channel": channel, "speaker": 0, "start": start, "end": end,
                 "transcript": text}] if text else []
    if end - start >= 2 * MIN_RETRY_S:
        mid = (start + end) / 2
        return (_transcribe_window(wav, channel, start, mid, tmp, tag + "a")
                + _transcribe_window(wav, channel, mid, end, tmp, tag + "b"))
    return [{"channel": channel, "speaker": 0, "start": start, "end": end,
             "transcript": "[transcription failed for this part]", "failed": True}]


def transcribe(audio: Path, cfg: dict) -> dict:
    if not shutil.which("voxtype"):
        raise RuntimeError("voxtype not installed: install Omarchy's dictation (voxtype), "
                           "or switch transcription_provider to a cloud service")
    meta = info()
    whisper = meta.get("engine") == "whisper"
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        # The mic copy gets the rumble high-pass in the split; the far copy is
        # left exactly as recorded. audio.opus itself is never rewritten.
        left, right = audio_mod.split_stereo_to_mono_wavs(audio, tmp)
        # Mic noise reduction (spitball/denoise.py): a further temp copy of
        # the mic channel, made only when the setting (and, for "auto", the
        # measured noise floor) says so. `mic_denoise` records what ran.
        mic, mic_record = denoise.prepare_mic(left, cfg, tmp / "channel-0-denoised.wav")
        utterances = (_transcribe_channel(mic, 0, tmp, vad=whisper)
                      + _transcribe_channel(right, 1, tmp, vad=whisper))
    return {"provider": "local", "model": meta.get("model") or "", "utterances": utterances,
            "mic_denoise": mic_record}


def ready(cfg: dict) -> str:
    """Cheap, no-network readiness check: is voxtype even on PATH? Which
    model/engine it's configured with is the user's own business (via
    voxtype's own setup), so that's never a setup_needed reason."""
    return "" if shutil.which("voxtype") else NOT_INSTALLED_MESSAGE


def check(cfg: dict) -> dict:
    meta = info()
    if not meta["installed"]:
        return {"ok": False, "message": meta["message"]}
    return {"ok": True, "message": meta["message"]}


# ------------------------------------------------------------- model picker

def _languages_for(engine: str, name: str) -> str:
    if engine == "parakeet":
        if "v2" in name or "unified-en" in name:
            return "English"
        return "25 European languages"
    return "English" if name.endswith(".en") else "~99 languages"


def _installed_size_mb(engine: str, name: str) -> int | None:
    """The real on-disk size of an installed model -- more trustworthy than
    a guess, and free to compute since voxtype already told us it's
    installed."""
    if engine == "whisper":
        p = MODEL_DIR / f"ggml-{name}.bin"
        return round(p.stat().st_size / 1048576) if p.exists() else None
    d = MODEL_DIR / name
    if not d.is_dir():
        return None
    total = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
    return round(total / 1048576) if total else None


def list_models() -> list:
    """Every whisper/parakeet model voxtype knows how to download, each with
    installed state (from voxtype itself, `voxtype info models --json`),
    size (measured if installed, else a known/estimated figure or null),
    language coverage, whether it's the recommended pick, and whether it's
    the currently active model. Spitball never downloads any of these
    itself -- see `set_model()`/`bin/spitball-upgrade-parakeet`."""
    if not shutil.which("voxtype"):
        return []
    try:
        raw = subprocess.run(["voxtype", "info", "models", "--json"],
                              capture_output=True, text=True, timeout=15).stdout
        catalog = json.loads(raw)
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    meta = info()
    active_engine, active_model = meta.get("engine", ""), meta.get("model", "")
    out = []
    for engine in _ONLY_ENGINES:
        engine_data = catalog.get("engines", {}).get(engine)
        if not engine_data:
            continue
        for m in engine_data.get("models", []):
            name = m.get("name", "")
            installed = bool(m.get("installed"))
            size_mb = _installed_size_mb(engine, name) if installed else KNOWN_SIZE_MB.get(name)
            out.append({
                "name": name, "engine": engine, "installed": installed, "size_mb": size_mb,
                "languages": _languages_for(engine, name),
                "recommended": name == RECOMMENDED_MODEL,
                "active": engine == active_engine and name == active_model,
            })
    return out


def _engine_for_model(name: str) -> str:
    return "parakeet" if name.startswith("parakeet") else "whisper"


def _resolve_terminal_command(script: Path, args: list) -> list | None:
    """Best available way to open a floating terminal running our script,
    the Omarchy way first (gives the nice presentation wrapper), falling
    back to a generic terminal launcher, per spec order."""
    quoted = " ".join(["bash", str(script), *args])
    if shutil.which("omarchy-launch-floating-terminal-with-presentation"):
        return ["omarchy-launch-floating-terminal-with-presentation", quoted]
    if shutil.which("xdg-terminal-exec"):
        return ["xdg-terminal-exec", "--", "bash", str(script), *args]
    term = os.environ.get("TERMINAL")
    if term and shutil.which(term):
        return [term, "-e", "bash", str(script), *args]
    return None


# ---------------------------------------------------------------- model.json

def model_state_path() -> Path:
    """`$XDG_RUNTIME_DIR/spitball/model.json` -- always read fresh off
    `config.RUNTIME_DIR` (never cached at import time) so test isolation via
    `tests/testutil.isolated_runtime()`, which reassigns that module
    attribute, redirects this too."""
    return config.RUNTIME_DIR / MODEL_STATE_FILENAME


def _write_model_state(name: str, state: str, message: str = "",
                        done_bytes: int = 0, total_bytes: int = 0) -> None:
    assert state in MODEL_SWITCH_STATES, f"unknown model switch state: {state!r}"
    config.atomic_write(model_state_path(), json.dumps({
        "name": name, "state": state, "message": message,
        "done_bytes": done_bytes, "total_bytes": total_bytes,
    }))


def read_model_state() -> dict | None:
    """For tests/introspection -- the UIs read the file directly via a
    FileView watch (see Widget.qml/SettingsPanel.qml), not this."""
    try:
        data = json.loads(model_state_path().read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------ set-model (UI)

def _polkit_agent_likely_available(ping_runner=subprocess.run) -> bool:
    """Best-effort: true if the Omarchy shell answers its own `shell ping`
    IPC call. Omarchy's polkit authentication dialog
    (`/usr/share/omarchy/shell/plugins/polkit/PolkitAgent.qml`) is a
    `service`-kind plugin embedded in that same shell process, not a
    separate agent daemon -- there's no PolicyKit1 bus name or standalone
    process to probe, so "is the shell up" is the closest real signal for
    "will pkexec actually be able to draw a prompt, or fail outright with no
    TTY and no agent"."""
    try:
        r = ping_runner(["omarchy-shell", "-q", "shell", "ping"],
                         capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def set_model(name: str, popen=subprocess.Popen, ping_runner=subprocess.run,
               script: Path = SET_MODEL_SCRIPT) -> dict:
    """Starts switching voxtype to `name` and returns immediately -- see this
    module's docstring and CONTRACT.md. Always non-blocking and always
    succeeds at starting *something* unless there's truly no way to launch
    anything (no pkexec/agent AND no terminal launcher). Never runs sudo or
    pkexec itself -- only the detached worker (or the fallback script) does.

    Returns {"ok": bool, "mode": "background"|"terminal"|"none", "message": str}.
    """
    if shutil.which("pkexec") and _polkit_agent_likely_available(ping_runner):
        engine_changes = _engine_for_model(name) != info().get("engine", "")
        _write_model_state(name, "switching-engine" if engine_changes else "downloading",
                            message="Starting…")
        cmd = ["/usr/bin/python3", "-I", str(SPITBALL_CLI), "local", "_set-model-worker", name]
        popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
              start_new_session=True)
        return {"ok": True, "mode": "background", "message": f"switching to {name} in the background"}

    engine = _engine_for_model(name)
    cmd = _resolve_terminal_command(script, [name, engine])
    if cmd is None:
        return {"ok": False, "mode": "none",
                "message": "no graphical password helper and no terminal launcher available "
                           "(omarchy-launch-floating-terminal-with-presentation, xdg-terminal-exec, or $TERMINAL)"}
    popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
          start_new_session=True)
    _write_model_state(name, "terminal",
                        message="No graphical password prompt available -- opened a terminal instead.")
    return {"ok": True, "mode": "terminal", "message": f"opened a terminal to switch voxtype to {name}"}


# ------------------------------------------------------------ streaming

def voxtype_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "voxtype" / "config.toml"


def _ensure_streaming_windows(path: Path) -> None:
    """Adds any missing STREAMING_WINDOWS key to voxtype's [parakeet] table
    (creating the table if there isn't one). Never overwrites a value the
    user already set, and leaves every other line alone."""
    text = path.read_text() if path.exists() else ""
    lines = text.splitlines(keepends=True)
    header = next((i for i, l in enumerate(lines) if l.strip() == "[parakeet]"), None)
    if header is None:
        missing = [f"{k} = {v}\n" for k, v in STREAMING_WINDOWS]
        sep = "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + sep + "[parakeet]\n" + "".join(missing))
        return
    end = next((i for i in range(header + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
    present = {l.split("=", 1)[0].strip() for l in lines[header + 1:end] if "=" in l}
    missing = [f"{k} = {v}\n" for k, v in STREAMING_WINDOWS if k not in present]
    if missing:
        path.write_text("".join(lines[:header + 1] + missing + lines[header + 1:]))


def apply_streaming_config(name: str, run=subprocess.run, path: Path | None = None) -> str:
    """Turns voxtype's `parakeet.streaming` on for a streaming-capable model
    (writing the window sizes it needs first) and off for any other parakeet
    model. Returns "" on success, else a short error message."""
    streaming = name in STREAMING_MODELS
    try:
        if streaming:
            _ensure_streaming_windows(path or voxtype_config_path())
        r = run(["voxtype", "config", "set", "parakeet.streaming", "true" if streaming else "false"],
                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return f"Couldn't set voxtype's streaming mode: {e}"
    if getattr(r, "returncode", 1) != 0:
        return _first_line(getattr(r, "stderr", "") or "") or "Couldn't set voxtype's streaming mode"
    return ""


# ------------------------------------------------------------ set-model (worker)

def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def run_set_model_worker(name: str, run=subprocess.run, popen=subprocess.Popen) -> None:
    """Blocking. Runs the actual switch to completion, writing every step to
    model.json. Only ever called inside the detached child process
    `set_model()` spawns (`spitball local _set-model-worker <name>`) --
    never from Spitball's own long-lived daemon or CLI invocation, and never
    exercised for real in tests (`run`/`popen` are always mocked there)."""
    try:
        target_engine = _engine_for_model(name)
        current_engine = info().get("engine", "")
        if current_engine and current_engine != target_engine:
            _write_model_state(name, "switching-engine",
                                message="Switching voxtype's engine (enter your password if asked)…")
            action = "--enable" if target_engine == "parakeet" else "--disable"
            r = run(["pkexec", "voxtype", "setup", "onnx", action],
                    capture_output=True, text=True, timeout=300)
            if r.returncode != 0:
                message = ("Password prompt canceled" if r.returncode == 126
                           else (_first_line(r.stderr) or "Couldn't switch voxtype's engine"))
                _write_model_state(name, "error", message=message)
                return

        _write_model_state(name, "downloading", message=f"Downloading {name}…")
        proc = popen(["voxtype", "setup", "--download", "--model", name, "--activate",
                      "--progress-format", "json"],
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        error_message = ""
        for line in proc.stdout or []:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get("event")
            if kind == "progress":
                _write_model_state(name, "downloading", message=f"Downloading {name}…",
                                    done_bytes=int(event.get("bytes") or 0),
                                    total_bytes=int(event.get("total") or 0))
            elif kind == "error":
                error_message = event.get("message", "")
            elif kind == "done":
                _write_model_state(name, "activating", message=f"Activating {name}…")

        returncode = proc.wait(timeout=60)
        if returncode != 0:
            if not error_message and proc.stderr:
                error_message = _first_line(proc.stderr.read())
            _write_model_state(name, "error", message=error_message or "Download or activation failed")
            return

        if target_engine == "parakeet":
            error = apply_streaming_config(name, run=run)
            if error:
                _write_model_state(name, "error", message=error)
                return

        active = run(["systemctl", "--user", "is-active", "--quiet", "voxtype"], timeout=10)
        if getattr(active, "returncode", 1) == 0:
            _write_model_state(name, "restarting", message="Restarting voxtype…")
            run(["systemctl", "--user", "restart", "voxtype"], timeout=30)

        _write_model_state(name, "done", message=f"Switched to {name}")
    except Exception as e:  # noqa: BLE001 -- last-resort: model.json must never be left mid-step
        _write_model_state(name, "error", message=str(e))
