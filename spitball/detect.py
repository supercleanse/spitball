"""Call detection: which known call apps currently hold the microphone.

PipeWire (through its PulseAudio layer) lists every open recording stream as a
"source output". A call app opening the mic is our signal that a call started; the
stream closing is the signal that it ended. `pactl subscribe` tells us when to look.
"""
from __future__ import annotations

import json
import subprocess
import threading

OWN_APP_NAME = "spitball"  # ffmpeg's stream name, so we never detect ourselves


def _source_names() -> dict:
    """Map source index -> source name, to skip streams reading a speaker monitor."""
    try:
        out = subprocess.run(["pactl", "-f", "json", "list", "sources", "short"],
                             capture_output=True, text=True, timeout=5).stdout
        return {int(s["index"]): s.get("name", "") for s in json.loads(out or "[]")}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}


def call_apps(call_apps_cfg: dict) -> set:
    """Display names of known call apps that have the mic open right now."""
    try:
        out = subprocess.run(["pactl", "-f", "json", "list", "source-outputs"],
                             capture_output=True, text=True, timeout=5).stdout
        streams = json.loads(out or "[]")
    except (OSError, ValueError, subprocess.SubprocessError):
        return set()
    sources = _source_names() if streams else {}
    found = set()
    for s in streams:
        props = s.get("properties", {})
        name = str(props.get("application.name", ""))
        binary = str(props.get("application.process.binary", ""))
        media = str(props.get("media.name", ""))
        if name == OWN_APP_NAME or "peak detect" in media.lower():
            continue
        if sources.get(s.get("source"), "").endswith(".monitor"):
            continue  # reading speaker output (visualizers, our own recorder), not the mic
        hay = f"{binary} {name}".lower()
        for key, display in call_apps_cfg.items():
            if key.lower() in hay:
                found.add(display)
                break
    return found


class Watcher(threading.Thread):
    """Runs `pactl subscribe` and pokes `on_change` whenever a recording stream
    appears or disappears. The daemon also rescans on its own 1s tick, so a dead
    subscriber costs responsiveness, never correctness."""

    def __init__(self, on_change):
        super().__init__(daemon=True, name="pactl-subscribe")
        self.on_change = on_change

    def run(self):
        while True:
            try:
                proc = subprocess.Popen(["pactl", "subscribe"], stdout=subprocess.PIPE,
                                        text=True, bufsize=1)
                for line in proc.stdout:
                    if "source-output" in line:
                        self.on_change()
            except OSError:
                pass
            threading.Event().wait(5)  # pactl died (PipeWire restart); resubscribe
