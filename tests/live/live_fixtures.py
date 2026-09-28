"""Helpers for the live test suite: downloads/builds are cached under
tests/.cache/ (gitignored) so a re-run of `tests/run.sh --live` doesn't
re-fetch or re-encode anything that's already there."""
from __future__ import annotations

import subprocess
import urllib.request
from pathlib import Path

CACHE = Path(__file__).resolve().parent.parent / ".cache"

SPACEWALK_URL = "https://dpgr.am/spacewalk.wav"
BUELLER_URL = "https://static.deepgram.com/examples/Bueller-Life-moves-pretty-fast.wav"


def _download(url: str, dest: Path) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    CACHE.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=30) as resp:
        dest.write_bytes(resp.read())
    return dest


def two_channel_sample(delay_ms: int = 3000) -> Path:
    """A two-channel opus file: left = spacewalk.wav ("me"), right =
    Bueller.wav delayed a few seconds ("them") -- enough for Deepgram's
    multichannel diarization to clearly separate two distinct speakers."""
    out = CACHE / "two_channel_sample.opus"
    if out.exists() and out.stat().st_size > 0:
        return out
    CACHE.mkdir(parents=True, exist_ok=True)
    left = _download(SPACEWALK_URL, CACHE / "spacewalk.wav")
    right = _download(BUELLER_URL, CACHE / "bueller.wav")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(left), "-i", str(right),
        "-filter_complex",
        # Bueller.wav is itself stereo -- downmix it to mono *before* delaying
        # and merging, or amerge's "inputs=2" (2 source streams, not 2
        # channels) would sum to 3 channels and the trailing -ac 2 would
        # downmix that back to 2, bleeding both sides' words into both
        # output channels (confirmed empirically: without this, Deepgram
        # transcribed identical text on channel 0 and channel 1).
        f"[1:a]pan=mono|c0=0.5*c0+0.5*c1,adelay={delay_ms}[right];"
        "[0:a][right]amerge=inputs=2[a]",
        "-map", "[a]", "-ac", "2", "-ar", "16000",
        "-c:a", "libopus", "-b:a", "32k", "-application", "voip",
        str(out),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=60)
    return out
