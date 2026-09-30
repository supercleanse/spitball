"""Mic noise reduction for the transcriber (docs/SPEC-v2.md section 3).

Everything here works on a TEMPORARY COPY of the mic channel (channel 0). The
recording itself, `audio.opus`, is never modified, and the far channel is
never denoised: it is a monitor of the call app's already-processed output,
and the evidence says enhancing clean audio makes transcripts worse, not
better (see the research behind the spec). So:

- `mic_denoise: "off"`  -- the transcriber hears the mic copy as recorded
  (after the rumble high-pass that `spitball/audio.py` always applies).
- `mic_denoise: "auto"` (default) -- the mic copy's background level is
  measured and denoised only when it sits above `mic_noise_floor_db`
  (default -45 dBFS). A quiet headset call is left alone; a fan, a cafe, or a
  laptop mic in a busy room gets the filter.
- `mic_denoise: "on"`   -- always denoise the mic copy.

The filter is ffmpeg's `arnndn` (RNNoise) with the model vendored under
`models/rnnoise/` and `mix=0.7` (70% denoised, 30% original: the blend the
EUSIPCO 2024 paper found is the one configuration that beats raw audio
rather than hurting it). If the model file is missing or `arnndn` fails,
ffmpeg's built-in `afftdn` is the fallback. `anlmdn` is never used: it
aborts in ffmpeg 9.0.1.

Whichever mode actually ran is recorded in the transcript's metadata
(`.transcript.json` / `.live.json` gain a `mic_denoise` block, see
CONTRACT.md) so a transcript can always be read against what the model
heard. `spitball reprocess --retranscribe` runs this again with the current
setting.

The noise floor is the 10th percentile of 50 ms frame RMS levels over the
channel (`audio.measure_levels`): the quietest tenth of a call's mic is the
room between your words, whether or not `silencedetect`'s fixed gate finds
any formal silence in it (steady noise above -35 dB defeats that gate, which
is exactly the case this feature exists for). A muted mic measures as
digital silence (-100) and never triggers.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from . import audio as audio_mod
from . import config

MODES = ("off", "auto", "on")
DEFAULT_MODE = "auto"
DEFAULT_THRESHOLD_DB = -45.0
MIN_THRESHOLD_DB = -80.0
MAX_THRESHOLD_DB = -20.0
MIX = 0.7
MODEL_PATH = config.PROGRAM_ROOT / "models" / "rnnoise" / "sh.rnnn"
AFFTDN_CHAIN = "afftdn=nr=12:nf=-40:tn=1"

FLOOR_PERCENTILE = 0.10
SPEECH_PERCENTILE = 0.90
MIN_FLOOR_FRAMES = 40          # 2 s of 50 ms frames before an estimate counts
SPEECH_MARGIN_DB = 6.0         # a window whose peak frame sits closer than this to the floor has no speech in it
SILENCE_GATE_DB = -35          # audio.detect_silence's default gate
SILENCE_GATE_ABOVE_FLOOR_DB = 10.0
SILENCE_GATE_MAX_DB = -20

LIVE_WINDOW_S = 60.0           # the live gate judges the last minute of mic audio
HYSTERESIS_DB = 3.0            # the live gate turns off only this far below the threshold


# ------------------------------------------------------------------ settings

def mode(cfg: dict | None) -> str:
    """The configured mode; anything unknown reads as the default."""
    value = str((cfg or {}).get("mic_denoise", DEFAULT_MODE)).strip().lower()
    return value if value in MODES else DEFAULT_MODE


def threshold_db(cfg: dict | None) -> float:
    """`mic_noise_floor_db`, clamped to a sane dBFS range."""
    try:
        value = float((cfg or {}).get("mic_noise_floor_db", DEFAULT_THRESHOLD_DB))
    except (TypeError, ValueError):
        value = DEFAULT_THRESHOLD_DB
    return min(MAX_THRESHOLD_DB, max(MIN_THRESHOLD_DB, value))


# ------------------------------------------------------------------ filter chain

def escape_filter_path(path: Path | str) -> str:
    """Escapes a file path for use as a filter option value inside an ffmpeg
    filtergraph string. Two levels of escaping apply there (the graph parser,
    then the option tokenizer), so the single-level escape is applied twice:
    `'` becomes `\\\\\\'`, and so on -- verified against ffmpeg 9 with a
    directory named `weird dir:with,chars;and'quote[x]`."""
    def once(text: str) -> str:
        return "".join("\\" + ch if ch in "\\':,;[]=" else ch for ch in text)
    return once(once(str(path)))


def filter_chain(model_path: Path | None = None) -> tuple[str, str]:
    """(filtergraph, name): `arnndn` with the vendored model when the file is
    there, else the `afftdn` fallback. The name is what the transcript
    metadata records."""
    model = MODEL_PATH if model_path is None else model_path
    if model.is_file():
        return f"arnndn=m={escape_filter_path(model)}:mix={MIX}", "arnndn"
    return AFFTDN_CHAIN, "afftdn"


TAIL_PAD_S = 0.2


def _run_filter(src: Path, dst: Path, chain: str, duration: float) -> None:
    """One ffmpeg pass: `chain` over `src` into a 16 kHz `dst`.

    The input is padded with a little silence first and the output cut back
    to `duration`: ffmpeg 9's `arnndn` flushes its final partial frame
    against an uninitialized history buffer, which (measured here) leaves
    ~176 NaN samples at the very end of the file -- a full-scale click once
    written as 16-bit PCM, on every clip whose length isn't a multiple of
    its 10 ms frame, i.e. every live tail. Pushing the real audio's end away
    from the flush and trimming the pad off again avoids it entirely."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-i", str(src), "-af", f"apad=pad_dur={TAIL_PAD_S},{chain}"]
    if duration > 0:
        cmd += ["-t", f"{duration:.6f}"]
    cmd += ["-ar", "16000", str(dst)]
    subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=600)


def apply(src: Path, dst: Path, model_path: Path | None = None) -> str:
    """Writes the denoised copy of `src` to `dst` and returns the name of the
    filter that produced it. Tries RNNoise first, then `afftdn` if RNNoise's
    model is missing or ffmpeg refuses it; raises RuntimeError only when the
    fallback fails too."""
    chain, name = filter_chain(model_path)
    duration = audio_mod.ffprobe_duration(src)
    try:
        _run_filter(src, dst, chain, duration)
        return name
    except (OSError, subprocess.SubprocessError) as first:
        if name == "afftdn":
            raise RuntimeError(f"afftdn failed: {_err(first)}") from first
        try:
            _run_filter(src, dst, AFFTDN_CHAIN, duration)
            return "afftdn"
        except (OSError, subprocess.SubprocessError) as second:
            raise RuntimeError(f"arnndn failed ({_err(first)}); afftdn failed ({_err(second)})") from second


def _err(e: Exception) -> str:
    stderr = getattr(e, "stderr", "") or ""
    return (stderr.strip().splitlines() or [str(e)])[-1][:200]


# ------------------------------------------------------------------ noise floor

def _percentile(levels: list[float], q: float) -> float | None:
    if not levels:
        return None
    ordered = sorted(levels)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def noise_floor_db(levels: list[float]) -> float | None:
    """The 10th percentile of per-frame RMS levels (dBFS); None until there
    are at least MIN_FLOOR_FRAMES frames to judge from."""
    if len(levels) < MIN_FLOOR_FRAMES:
        return None
    return _percentile(levels, FLOOR_PERCENTILE)


def speech_level_db(levels: list[float]) -> float | None:
    """The 90th percentile -- roughly where the talking sits. Recorded next
    to the floor so the gate can be tuned against real calls later."""
    if len(levels) < MIN_FLOOR_FRAMES:
        return None
    return _percentile(levels, SPEECH_PERCENTILE)


def should_apply(mode_value: str, floor: float | None, threshold: float) -> bool:
    if mode_value == "on":
        return True
    if mode_value == "off":
        return False
    return floor is not None and floor > threshold


def adaptive_silence_db(floor: float | None) -> int:
    """The `silencedetect` gate for a channel whose floor is known: the
    default -35 dB, raised to 10 dB above the floor (never past -20) when
    steady noise would otherwise sit above the gate and leave the channel
    with no detectable pauses at all."""
    if floor is None:
        return SILENCE_GATE_DB
    return int(min(SILENCE_GATE_MAX_DB, max(SILENCE_GATE_DB, round(floor + SILENCE_GATE_ABOVE_FLOOR_DB))))


def has_speech(levels: list[float], start: float, end: float, floor: float | None,
               frame_s: float = audio_mod.LEVEL_FRAME_S) -> bool:
    """True unless every frame in [start, end) sits within SPEECH_MARGIN_DB of
    the floor -- a stretch with nothing louder than the room in it. Used on
    the Whisper path, which hallucinates text over noise-only audio."""
    if floor is None or not levels:
        return True
    lo = max(0, int(start / frame_s))
    hi = min(len(levels), max(lo + 1, int(end / frame_s + 0.999)))
    frames = levels[lo:hi]
    if not frames:
        return True
    return max(frames) > floor + SPEECH_MARGIN_DB


# ------------------------------------------------------------------ post-call path

def record_for(mode_value: str, applied: bool, filter_name: str | None, floor: float | None,
               threshold: float, speech: float | None = None, error: str = "") -> dict:
    """The `mic_denoise` block written into the transcript metadata."""
    out = {
        "mode": mode_value,
        "applied": bool(applied),
        "filter": filter_name if applied else None,
        "noise_floor_db": None if floor is None else round(floor, 1),
        "threshold_db": round(threshold, 1),
    }
    if speech is not None:
        out["speech_level_db"] = round(speech, 1)
    if error:
        out["error"] = error
    return out


def prepare_mic(wav: Path, cfg: dict | None, dst: Path,
                levels: list[float] | None = None) -> tuple[Path, dict]:
    """Decides for the mic copy `wav` and returns (path to transcribe, the
    `mic_denoise` record). `dst` is where a denoised copy goes when one is
    made; `wav` itself is never rewritten. Never raises: a filter failure
    falls back to the untouched copy and is noted in the record."""
    mode_value = mode(cfg)
    threshold = threshold_db(cfg)
    floor = speech = None
    if mode_value != "off":
        if levels is None:
            levels = audio_mod.measure_levels(wav)
        floor = noise_floor_db(levels)
        speech = speech_level_db(levels)
    if not should_apply(mode_value, floor, threshold):
        return wav, record_for(mode_value, False, None, floor, threshold, speech)
    try:
        name = apply(wav, dst)
    except RuntimeError as e:
        return wav, record_for(mode_value, False, None, floor, threshold, speech, error=str(e))
    return dst, record_for(mode_value, True, name, floor, threshold, speech)


# ------------------------------------------------------------------ live path

class LiveGate:
    """The `auto` decision for the live transcriber, which sees the mic a few
    seconds at a time. Frame levels from each tail clip accumulate into a
    rolling window (the last LIVE_WINDOW_S of mic audio); the gate turns on
    once the rolling floor rises above the threshold and off only once it
    drops HYSTERESIS_DB below it, so a call that hovers at the threshold
    doesn't flip the filter on and off between lines. `on`/`off` modes are
    constant, and skip the measurement entirely."""

    def __init__(self, cfg: dict | None):
        self.mode = mode(cfg)
        self.threshold = threshold_db(cfg)
        self.active = self.mode == "on"
        self.ever_applied = False
        self.filter_name: str | None = None
        self.error = ""
        self._levels: list[float] = []
        self._max_frames = int(LIVE_WINDOW_S / audio_mod.LEVEL_FRAME_S)

    @property
    def measures(self) -> bool:
        return self.mode == "auto"

    def feed(self, levels: list[float] | None) -> bool:
        """Adds one tail clip's frame levels (ignored unless `auto`) and
        returns whether the next clip should be denoised."""
        if self.mode == "auto" and levels:
            self._levels.extend(levels)
            del self._levels[:-self._max_frames]
            floor = noise_floor_db(self._levels)
            if floor is not None:
                if not self.active and floor > self.threshold:
                    self.active = True
                elif self.active and floor < self.threshold - HYSTERESIS_DB:
                    self.active = False
        return self.active

    def applied(self, filter_name: str) -> None:
        self.ever_applied = True
        self.filter_name = filter_name

    def failed(self, error: str) -> None:
        self.error = error

    def record(self) -> dict:
        floor = noise_floor_db(self._levels) if self.mode == "auto" else None
        speech = speech_level_db(self._levels) if self.mode == "auto" else None
        return record_for(self.mode, self.ever_applied, self.filter_name, floor, self.threshold,
                          speech, error=self.error)
