"""Shared audio-shaping helpers for transcription providers: splitting a
stereo call recording into two mono channels, finding speech windows via
silence detection, cutting clips, and the chunk-size math a provider with an
upload limit needs. Everything here shells out to ffmpeg/ffprobe (already a
hard dependency, per README) -- no extra tools needed.

The `local` provider (spitball/providers/local.py) uses the segmentation
helpers (ffprobe_duration, split_stereo_to_mono_wavs, extract_clip,
detect_silence, speech_windows). encode_opus/chunk_ranges aren't used by any
phase-1 provider -- they're kept for the phase-2 OpenAI-compatible provider
(see docs/ROADMAP.md / docs/phase2/), which needs them for its 25 MB upload
limit.
"""
from __future__ import annotations

import math
import re
import subprocess
from pathlib import Path


def ffprobe_duration(path: Path) -> float:
    """Length of an audio file in seconds; 0.0 if it can't be read."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30).stdout.strip()
        return float(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def split_stereo_to_mono_wavs(src: Path, dst_dir: Path) -> tuple[Path, Path]:
    """Splits a stereo file into two mono 16 kHz WAVs: left = channel 0 (mic),
    right = channel 1 (far side). One ffmpeg invocation, two outputs."""
    left = dst_dir / "channel-0.wav"
    right = dst_dir / "channel-1.wav"
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(src),
        "-filter_complex", "[0:a]channelsplit=channel_layout=stereo[left][right]",
        "-map", "[left]", "-ar", "16000", str(left),
        "-map", "[right]", "-ar", "16000", str(right),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=600)
    return left, right


def extract_clip(src: Path, start: float, end: float, dst: Path) -> None:
    """Cuts [start, end) seconds out of `src` into `dst`, re-encoding (not
    stream-copying) so odd start times land on exact sample boundaries."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(src), str(dst)]
    subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=120)


def extract_channel_clip(src: Path, channel: int, start: float, end: float, dst: Path) -> None:
    """Cuts [start, end) out of ONE channel of a stereo file straight from the
    source, in one ffmpeg call (a mono `pan` filter picks the channel, same
    invocation trims to the range). `channel` is 0 (left, mic) or 1 (right,
    far side), matching recorder.py's layout. (`channelsplit` -- what
    split_stereo_to_mono_wavs uses -- needs both outputs mapped or ffmpeg
    refuses to build the filtergraph; `pan` needs only the one channel we
    actually want, which is simpler here.)

    Used by the live transcriber (spitball/live.py), which pulls each closed
    segment directly from the growing stereo recording rather than
    maintaining a running per-channel file of its own. Also works on a file
    ffmpeg is still writing (the recorder hasn't stopped yet) -- a torn
    trailing Ogg page just means `end` can't reach all the way to the true
    live edge yet, not a hard failure; the caller clamps `end` to what
    ffprobe currently reports decodable."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(src),
           "-af", f"pan=mono|c0=c{channel}", "-ar", "16000", str(dst)]
    subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=120)


def encode_opus(src_wav: Path, dst_ogg: Path, bitrate: str = "24k") -> None:
    """Encodes a WAV to an Ogg/Opus file at the given bitrate -- used for the
    OpenAI provider's upload (small files, well under its 25 MB cap)."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-i", str(src_wav), "-c:a", "libopus", "-b:a", bitrate,
           "-application", "voip", str(dst_ogg)]
    subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=600)


_SILENCE_START_RE = re.compile(r"silence_start:\s*([\d.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*([\d.]+)")


def detect_silence(wav: Path, noise_db: int = -35, min_silence_s: float = 0.6) -> list[tuple[float, float]]:
    """Runs ffmpeg's silencedetect filter over `wav` and returns the silent
    spans as [(start, end), ...], parsed from its stderr log lines
    (`silence_start: N` / `silence_end: N | silence_duration: M`)."""
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-i", str(wav),
           "-af", f"silencedetect=noise={noise_db}dB:d={min_silence_s}",
           "-f", "null", "-"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    spans = []
    pending_start = None
    for line in result.stderr.splitlines():
        m = _SILENCE_START_RE.search(line)
        if m:
            pending_start = float(m.group(1))
            continue
        m = _SILENCE_END_RE.search(line)
        if m and pending_start is not None:
            spans.append((pending_start, float(m.group(1))))
            pending_start = None
    return spans


def raw_speech_segments(duration: float, silences: list[tuple[float, float]],
                         min_segment_s: float = 0.4) -> list[tuple[float, float]]:
    """The complement of `silences` within [0, duration], dropping slivers
    under `min_segment_s` -- the raw speech runs, each one still bounded by
    an actual detected pause on both sides (or the ends of the file). This is
    the part of speech_windows() below that finds individual pause-bounded
    runs, before they get merged into upload-sized windows -- used directly
    by speech_windows(), and by the live transcriber (spitball/live.py),
    which wants each run transcribed the moment a pause closes it rather
    than waiting for several to be bridged into one window."""
    if duration <= 0:
        return []
    # Complement of the silences, clipped to [0, duration].
    segments = []
    cursor = 0.0
    for s, e in sorted(silences):
        s, e = max(0.0, s), min(duration, e)
        if s > cursor:
            segments.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < duration:
        segments.append((cursor, duration))
    return [(s, e) for s, e in segments if e - s >= min_segment_s]


def speech_windows(duration: float, silences: list[tuple[float, float]],
                    max_window_s: float = 30.0, min_segment_s: float = 0.4) -> list[tuple[float, float]]:
    """Turns silent spans into the complementary speech segments (see
    raw_speech_segments above), then merges neighboring segments into windows
    of at most `max_window_s`. Segments under `min_segment_s` are dropped
    (matches the spec's "skip windows under 0.4s")."""
    segments = raw_speech_segments(duration, silences, min_segment_s)

    # Merge consecutive segments (bridging the silence between them) into
    # windows of at most max_window_s, so a call with many short utterances
    # doesn't turn into hundreds of tiny voxtype invocations.
    windows: list[tuple[float, float]] = []
    for s, e in segments:
        if windows and e - windows[-1][0] <= max_window_s:
            windows[-1] = (windows[-1][0], e)
        else:
            windows.append((s, e))
    # A segment with no pause long enough to split on (steady background noise,
    # echo from laptop speakers) can run for minutes. Local models fail on
    # clips that long, so cut every window down to max_window_s.
    return [piece for w in windows for piece in split_evenly(w, max_window_s)]


def split_evenly(window: tuple[float, float], max_s: float) -> list[tuple[float, float]]:
    """Cuts (start, end) into the fewest equal pieces no longer than max_s."""
    start, end = window
    n = max(1, math.ceil((end - start) / max_s - 1e-9))
    step = (end - start) / n
    return [(start + i * step, end if i == n - 1 else start + (i + 1) * step) for i in range(n)]


def chunk_ranges(duration: float, max_chunk_s: float = 1200.0) -> list[tuple[float, float]]:
    """Splits [0, duration) into consecutive chunks of at most `max_chunk_s`
    seconds each -- the ≤20-minute chunking math for an over-limit upload."""
    if duration <= 0:
        return []
    ranges = []
    start = 0.0
    while start < duration:
        end = min(start + max_chunk_s, duration)
        ranges.append((start, end))
        start = end
    return ranges
