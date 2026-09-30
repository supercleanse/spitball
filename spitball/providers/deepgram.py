"""Deepgram transcription provider (the original, and still the default).
Multichannel + diarize, so channel 1 (the far side) can carry several
distinct speakers -- deepgram tells them apart on its own; the local
provider does so only with the optional on-device split
(spitball/diarize.py), and otherwise treats the far side as one speaker.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from .. import config

DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"


def raw_transcribe(audio: Path, cfg: dict) -> dict:
    """The actual Deepgram API call. Returns Deepgram's own JSON shape
    (`results.utterances[...]`) unnormalized -- callers that want the
    provider-neutral shape should use transcribe() below."""
    key = config.secret(cfg, "deepgram_api_key", env="DEEPGRAM_API_KEY")
    if not key:
        raise RuntimeError("No Deepgram API key: set DEEPGRAM_API_KEY, or deepgram_api_key "
                           f"or deepgram_api_key_command in {config.CONFIG_FILE}")
    params = {
        "model": cfg["deepgram_model"], "multichannel": "true",
        "diarize": "true", "smart_format": "true", "punctuate": "true",
        "utterances": "true", "mip_opt_out": "true",
    }
    language = cfg.get("language") or "en"
    if language == "auto":
        params["detect_language"] = "true"  # nova-3 code-switching/auto-detect; no `language` param alongside
    else:
        params["language"] = language
    url = DEEPGRAM_URL + "?" + "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(url, data=audio.read_bytes(), method="POST", headers={
        "Authorization": "Token " + key,
        "Content-Type": "audio/ogg",
    })
    try:
        with urllib.request.urlopen(req, timeout=900) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Deepgram error {e.code}: {e.read()[:300].decode(errors='replace')}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Deepgram unreachable: {e.reason}")


def normalize(raw: dict, cfg: dict) -> dict:
    """Deepgram's own JSON -> the shared {provider, model, utterances} shape."""
    utts = raw.get("results", {}).get("utterances", [])
    return {
        "provider": "deepgram",
        "model": cfg.get("deepgram_model", "nova-3"),
        "utterances": [
            {"channel": u["channel"], "speaker": u.get("speaker", 0),
             "start": u["start"], "end": u["end"], "transcript": u.get("transcript", "")}
            for u in utts
        ],
    }


def transcribe(audio: Path, cfg: dict, hints: dict | None = None) -> dict:
    """`hints` (the invitee count) is unused: Deepgram takes no speaker
    count and splits the far channel on its own."""
    return normalize(raw_transcribe(audio, cfg), cfg)


def ready(cfg: dict) -> str:
    """Cheap, no-network readiness check for setup_needed: is a key
    configured at all (env, plain value, or a _command to run one)? Doesn't
    execute the _command -- that could itself hit a network (a password
    manager CLI, say), which setup_needed must never do."""
    import os
    if os.environ.get("DEEPGRAM_API_KEY"):
        return ""
    if cfg.get("deepgram_api_key") or cfg.get("deepgram_api_key_command"):
        return ""
    return "Add a Deepgram key"


def check(cfg: dict) -> dict:
    """Real network check (for the Settings panel's Test button): GET
    /v1/projects with the resolved key."""
    key = config.secret(cfg, "deepgram_api_key", env="DEEPGRAM_API_KEY")
    if not key:
        return {"ok": False, "message": "No Deepgram API key configured"}
    req = urllib.request.Request("https://api.deepgram.com/v1/projects",
                                  headers={"Authorization": "Token " + key})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        return {"ok": True, "message": "Deepgram key is valid"}
    except urllib.error.HTTPError as e:
        return {"ok": False, "message": f"Deepgram error {e.code}: {e.read()[:200].decode(errors='replace')}"}
    except urllib.error.URLError as e:
        return {"ok": False, "message": f"Deepgram unreachable: {e.reason}"}
