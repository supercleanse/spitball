"""A persistent, already-loaded speech model for the live transcript.

`voxtype transcribe` reloads its model from disk on every call (~2s each),
which is most of the live popup's delay. This module keeps one copy of
voxtype's active Parakeet model loaded for the whole call instead, in a
worker process (spitball/engine_worker.py) running under its own small venv
with onnx-asr + onnxruntime -- the rest of Spitball stays standard-library
only and runs under /usr/bin/python3 as before.

Entirely optional: `open_engine()` returns None whenever the venv isn't set
up (`spitball live setup`), voxtype isn't on a Parakeet model, or the worker
fails to load, and spitball/live.py falls back to per-segment `voxtype
transcribe` exactly as it did before this existed.

Paths: $SPITBALL_ENGINE_DIR (default ~/.local/share/spitball/live-engine)
holds `venv/` and `models/` (adapter directories for model layouts onnx-asr
can't read directly -- symlinks plus a generated vocab, never a copy of
voxtype's own files).
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
from pathlib import Path

from . import config
from .providers import local as local_provider

ENGINE_DIR = Path(os.environ.get("SPITBALL_ENGINE_DIR")
                  or (Path.home() / ".local" / "share" / "spitball" / "live-engine"))
WORKER_SCRIPT = config.PROGRAM_ROOT / "spitball" / "engine_worker.py"
PACKAGES = ("onnx-asr[cpu]>=0.12,<0.13", "sentencepiece")

# Measured on an Intel Core Ultra X9 378H (16 mixed performance/efficiency
# cores): 4 threads was fastest for both parakeet models -- onnxruntime's
# default (one per core) spreads work onto the slow cores and ran ~2x slower.
THREADS = 4
LOAD_TIMEOUT_S = 120.0       # first load reads a 2.4 GB model off disk
REQUEST_TIMEOUT_S = 30.0


def venv_python() -> Path:
    return ENGINE_DIR / "venv" / "bin" / "python"


def venv_present() -> bool:
    """Whether the shared venv exists at all. Not the same as installed():
    `spitball diarize setup` creates this same venv with only sherpa-onnx
    in it, which is no live engine."""
    return venv_python().exists()


def has_package(import_name: str) -> bool:
    """Whether a package is in the venv, by its import directory under
    site-packages -- checked on disk, not by importing, so a status call
    never has to start the venv's Python. Shared with spitball/diarize.py
    for its own package."""
    if not venv_present():
        return False
    venv = ENGINE_DIR / "venv"
    return any(p.is_dir() for p in venv.glob(f"lib/python*/site-packages/{import_name}"))


def installed() -> bool:
    """The venv exists AND onnx-asr is in it -- what the worker imports.
    A venv without it (diarize-only, or a half-finished `live setup`)
    reports not installed, so `live status`, the Live page's Install
    button, and open_engine() all agree with what can actually start."""
    return has_package("onnx_asr")


def model_dir_for(meta: dict) -> Path | None:
    """voxtype's active model directory, if it's a Parakeet model the worker
    can load. Whisper models (single ggml files) stay on the voxtype path."""
    if meta.get("engine") != "parakeet" or not meta.get("model"):
        return None
    d = local_provider.MODEL_DIR / meta["model"]
    return d if d.is_dir() else None


def venv_steps(packages: tuple) -> list:
    """The commands that create the venv (if needed) and install `packages`
    into it: uv when it's on PATH (much faster), else the stdlib venv + pip.
    Shared with spitball/diarize.py, whose add-on lives in this same venv."""
    venv = ENGINE_DIR / "venv"
    uv = shutil.which("uv")
    if uv:
        return [[uv, "venv", "--quiet", "--allow-existing", "--python", "/usr/bin/python3", str(venv)],
                [uv, "pip", "install", "--quiet", "--python", str(venv / "bin" / "python"), *packages]]
    # `python -m venv` over an existing venv leaves its site-packages alone.
    return [["/usr/bin/python3", "-m", "venv", str(venv)],
            [str(venv / "bin" / "python"), "-m", "pip", "install", "--quiet", *packages]]


def install(packages: tuple, run=subprocess.run) -> str:
    """Runs venv_steps(packages). Returns "" on success, else a one-line
    error (the installer's last output line)."""
    ENGINE_DIR.mkdir(parents=True, exist_ok=True)
    for cmd in venv_steps(packages):
        try:
            r = run(cmd, capture_output=True, text=True, timeout=900)
        except (OSError, subprocess.SubprocessError) as e:
            return f"{cmd[0]} failed: {e}"
        if r.returncode != 0:
            detail = (r.stderr or r.stdout or "").strip().splitlines()
            return detail[-1] if detail else f"{' '.join(cmd[:3])} failed"
    return ""


def setup(run=subprocess.run) -> tuple[bool, str]:
    """Creates the venv (or reuses one `spitball diarize setup` made) and
    installs PACKAGES into it -- never skipped on an existing venv, since
    a diarize-only venv has no onnx-asr yet. Returns (ok, message)."""
    error = install(PACKAGES, run)
    if error:
        return False, error
    return True, f"Live engine installed in {ENGINE_DIR / 'venv'}"


class Engine:
    """One worker process with the model loaded. Not thread-safe: the live
    transcriber's single thread is its only caller."""

    def __init__(self, proc: subprocess.Popen):
        self._proc = proc
        self._lines: queue.Queue = queue.Queue()
        self._next_id = 0
        threading.Thread(target=self._pump, name="live-engine-reader", daemon=True).start()

    def _pump(self) -> None:
        for line in self._proc.stdout:
            self._lines.put(line)
        self._lines.put(None)  # EOF: the worker exited

    def _read(self, timeout: float) -> dict | None:
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            return None
        if line is None:
            return None
        try:
            return json.loads(line)
        except ValueError:
            return {}

    @classmethod
    def open(cls, model_dir: Path, python: Path | None = None,
             script: Path = WORKER_SCRIPT, timeout: float = LOAD_TIMEOUT_S) -> Engine | None:
        python = python or venv_python()
        try:
            proc = subprocess.Popen(
                [str(python), "-I", str(script), str(model_dir), str(ENGINE_DIR / "models"), str(THREADS)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1)
        except OSError:
            return None
        engine = cls(proc)
        while True:
            msg = engine._read(timeout)
            if msg is None or "error" in msg:
                engine.close()
                return None
            if msg.get("ready"):
                return engine
            # anything else (a stray line from a library) -- keep waiting

    def transcribe(self, wav: Path, timeout: float = REQUEST_TIMEOUT_S) -> str | None:
        """The clip's text ("" for no speech), or None if the worker failed --
        in which case this engine is closed and the caller should fall back."""
        if self._proc.poll() is not None:
            return None
        self._next_id += 1
        req_id = self._next_id
        try:
            self._proc.stdin.write(json.dumps({"id": req_id, "wav": str(wav)}) + "\n")
            self._proc.stdin.flush()
        except (OSError, ValueError):
            self.close()
            return None
        while True:
            msg = self._read(timeout)
            if msg is None:
                self.close()
                return None
            if msg.get("id") != req_id:
                continue  # a late answer to an earlier, timed-out request
            if "error" in msg:
                return None
            return msg.get("text", "")

    def alive(self) -> bool:
        return self._proc.poll() is None

    def close(self) -> None:
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
        except OSError:
            pass
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()


def open_engine(cfg: dict) -> Engine | None:
    """The live transcriber's entry point: a loaded Engine, or None to use
    the per-segment voxtype path. `live_engine: false` in config.json turns
    it off even when installed."""
    if cfg.get("live_engine") is False or not installed():
        return None
    model_dir = model_dir_for(local_provider.info())
    if model_dir is None:
        return None
    return Engine.open(model_dir)
