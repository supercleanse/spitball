"""Transcription-provider dispatch. Every provider module exposes the same
three functions: transcribe(audio, cfg) -> normalized dict, ready(cfg) -> str
(cheap, no-network setup_needed check), and check(cfg) -> {"ok", "message"}
(a real, on-demand check, used by `spitball check transcription`).

Phase 1 ships two providers: "local" (voxtype, on-device, default) and
"deepgram". OpenAI-compatible/AssemblyAI/Soniox are phase 2 -- see
docs/ROADMAP.md and docs/phase2/ -- but the dispatch here is written so
adding one later is just another entry in _PROVIDERS.

The normalized shape every provider returns:
    {"provider": "deepgram", "model": "nova-3",
     "utterances": [{"channel": 0, "speaker": 0, "start": 1.2, "end": 4.8,
                      "transcript": "text"}]}
"""
from __future__ import annotations

from pathlib import Path

from . import deepgram, local

_PROVIDERS = {"local": local, "deepgram": deepgram}


def _module(cfg: dict, name: str | None = None):
    key = name or cfg.get("transcription_provider", "local")
    mod = _PROVIDERS.get(key)
    if mod is None:
        raise RuntimeError(f"unknown transcription_provider {key!r}")
    return mod


def transcribe(audio: Path, cfg: dict, hints: dict | None = None) -> dict:
    """`hints` is what the pipeline already knows about the call that a
    provider may use: {"far_speakers": N} (how many other people the
    calendar invite lists, or None) feeds the local speaker split. Deepgram
    ignores it."""
    return _module(cfg).transcribe(audio, cfg, hints=hints)


def check(cfg: dict, provider: str | None = None) -> dict:
    return _module(cfg, provider).check(cfg)


def setup_needed(cfg: dict) -> str:
    """Cheap, no-network readiness check for the currently-configured
    transcription provider only -- summaries degrade gracefully on their own
    (a missing/unreachable summary model just means "transcript only"), so
    they never block setup_needed."""
    try:
        return _module(cfg).ready(cfg)
    except RuntimeError as e:
        return str(e)
