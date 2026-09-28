"""Audio capture: one stereo Opus file per call.

Left channel = the default microphone (me). Right channel = everything playing
through the default speakers/headphones (everyone else). Keeping the two sides on
separate channels lets Deepgram label who said what without guessing.
"""
from __future__ import annotations

import signal
import subprocess
import time
from pathlib import Path

from .detect import OWN_APP_NAME


def _default(kind: str) -> str:
    try:
        return subprocess.run(["pactl", f"get-default-{kind}"], capture_output=True,
                              text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


class Recording:
    def __init__(self, path: Path, bitrate: str = "32k"):
        self.path = path
        self.started_at = time.time()
        mic = _default("source") or "default"
        sink = _default("sink")
        monitor = f"{sink}.monitor" if sink else "@DEFAULT_MONITOR@"
        pulse_in = ["-f", "pulse", "-name", OWN_APP_NAME, "-channels", "1",
                    "-sample_rate", "16000"]
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
               *pulse_in, "-i", mic,
               *pulse_in, "-i", monitor,
               "-filter_complex", "[0:a][1:a]amerge=inputs=2[a]", "-map", "[a]",
               "-c:a", "libopus", "-b:a", bitrate, "-application", "voip",
               str(path)]
        path.parent.mkdir(parents=True, exist_ok=True)
        self.log = open(path.with_name("ffmpeg.log"), "w")
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=self.log)

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self) -> float:
        """Stop cleanly (SIGINT lets ffmpeg finish the Ogg file). Returns seconds recorded."""
        if self.alive():
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.log.close()
        log = self.path.with_name("ffmpeg.log")
        if log.exists() and log.stat().st_size == 0:
            log.unlink()
        return time.time() - self.started_at
