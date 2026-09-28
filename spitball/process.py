"""After a call: audio -> Deepgram transcript -> LLM summary -> files.

Each call folder ends up with exactly three files: audio.opus, transcript.md,
summary.md. When `export_dir` is set, summary + transcript are also copied there as
one markdown file. Every step is re-runnable with `spitball reprocess <dir>`.
"""
from __future__ import annotations

import difflib
import json
import re
import shutil
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from . import config

DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"
MAX_TRANSCRIPT_CHARS = 180_000  # about 3 hours of talk


def _hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _slug(text: str, limit: int = 60) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:limit].rstrip("-") or "call"


# ------------------------------------------------------------------ transcription

def transcribe(audio: Path, cfg: dict) -> dict:
    key = config.secret(cfg, "deepgram_api_key", env="DEEPGRAM_API_KEY")
    if not key:
        raise RuntimeError("No Deepgram API key: set DEEPGRAM_API_KEY, or deepgram_api_key "
                           f"or deepgram_api_key_command in {config.CONFIG_FILE}")
    params = {
        "model": cfg["deepgram_model"], "language": "en", "multichannel": "true",
        "diarize": "true", "smart_format": "true", "punctuate": "true",
        "utterances": "true", "mip_opt_out": "true",
    }
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


def _drop_echo(utts: list) -> list:
    """On laptop speakers the mic also hears the other side, so channel 0 repeats
    channel 1. Drop a mic utterance that overlaps a far-side one in time and says
    mostly the same words."""
    far = [u for u in utts if u["channel"] == 1]
    kept = []
    for u in utts:
        if u["channel"] == 0:
            words = u["transcript"].lower().split()
            echo = False
            for f in far:
                if f["start"] - 1.5 <= u["start"] <= f["end"] + 1.5:
                    ratio = difflib.SequenceMatcher(None, words, f["transcript"].lower().split()).ratio()
                    if ratio >= 0.6:
                        echo = True
                        break
            if echo:
                continue
        kept.append(u)
    return kept


def build_transcript(dg: dict, cfg: dict) -> list:
    """[(start_seconds, speaker_label, text)] with consecutive lines merged."""
    utts = sorted(dg.get("results", {}).get("utterances", []), key=lambda u: u["start"])
    utts = _drop_echo([u for u in utts if u.get("transcript", "").strip()])
    far_speakers = sorted({u.get("speaker", 0) for u in utts if u["channel"] == 1})
    label = {}
    for n, spk in enumerate(far_speakers, 1):
        label[spk] = "Them" if len(far_speakers) == 1 else f"Speaker {n}"
    lines = []
    for u in utts:
        who = cfg["my_name"] if u["channel"] == 0 else label.get(u.get("speaker", 0), "Them")
        text = u["transcript"].strip()
        if lines and lines[-1][1] == who and u["start"] - lines[-1][3] < 4:
            s, w, t, _ = lines[-1]
            lines[-1] = (s, w, f"{t} {text}", u["end"])
        else:
            lines.append((u["start"], who, text, u["end"]))
    return [(s, w, t) for s, w, t, _ in lines]


# ------------------------------------------------------------------ summary

SUMMARY_SYSTEM = """You write meeting notes from a call transcript for {me}.
Plain, direct American English. No filler, no hype, no preamble.

Return markdown in exactly this shape:

# <A short, specific title for the call, 3-8 words>

## Summary
- 3 to 7 bullets covering what was discussed and concluded.

## Decisions
- One bullet per decision actually made. Write "None." if there were none.

## Action items
- [ ] <Owner>: <task> (<due date if one was said>)
Write "None." if there were none.

## Open questions
- Anything left unresolved. Write "None." if there were none.

Use people's names when the transcript makes them clear. {me} is the speaker
labeled "{me}". Do not invent facts, names, or dates that are not in the transcript."""


def _post_json(url: str, body: dict | None, key: str, timeout: int) -> dict:
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def summarize(transcript_text: str, meta: str, cfg: dict | None = None) -> str:
    """One chat completion against any OpenAI-compatible endpoint (Ollama, LM Studio,
    OpenAI, OpenRouter…). Raises on any failure; the caller keeps the transcript."""
    cfg = cfg or config.load()
    if not cfg.get("summary_enabled", True):
        raise RuntimeError("summaries are turned off (summary_enabled)")
    base = cfg["summary_base_url"].rstrip("/")
    key = config.secret(cfg, "summary_api_key")
    try:
        model = cfg.get("summary_model") or _post_json(f"{base}/models", None, key, 10)["data"][0]["id"]
        body = transcript_text
        if len(body) > MAX_TRANSCRIPT_CHARS:
            body = body[:MAX_TRANSCRIPT_CHARS] + "\n\n[transcript truncated]"
        me = cfg.get("my_name") or "Me"
        reply = _post_json(f"{base}/chat/completions", {
            "model": model,
            "temperature": 0.3,
            "max_tokens": 4000,
            "messages": [
                {"role": "system", "content": SUMMARY_SYSTEM.replace("{me}", me)},
                {"role": "user", "content": f"{meta}\n\nTranscript:\n\n{body}"},
            ],
        }, key, 600)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"summary model error {e.code} at {base}: {e.read()[:200].decode(errors='replace')}")
    except (urllib.error.URLError, TimeoutError) as e:
        raise RuntimeError(f"summary model unreachable at {base} ({getattr(e, 'reason', e)})")
    except (KeyError, IndexError, ValueError) as e:
        raise RuntimeError(f"summary model at {base} returned an unexpected reply ({e})")
    text = (reply.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()  # reasoning models
    if not text:
        raise RuntimeError(f"summary model at {base} returned no text")
    return text


# ------------------------------------------------------------------ pipeline

def process(call_dir: Path, meta: dict, cfg: dict | None = None, notify=None) -> dict:
    """Transcribe + summarize one call folder. Returns {dir, title, summary}.
    `meta` holds app/started_at/duration (also saved as meta.json for reprocessing)."""
    cfg = cfg or config.load()
    audio = call_dir / "audio.opus"
    meta_path = call_dir / ".meta.json"
    if meta:
        meta_path.write_text(json.dumps(meta))
    else:
        meta = json.loads(meta_path.read_text())

    started = datetime.fromtimestamp(meta["started_at"])
    header = (f"**Date:** {started:%B %-d, %Y, %-I:%M %p}  \n"
              f"**App:** {meta.get('app') or 'Manual'}  \n"
              f"**Length:** {_hms(meta['duration'])}")

    dg_path = call_dir / ".deepgram.json"
    if dg_path.exists():
        dg = json.loads(dg_path.read_text())
    else:
        if notify:
            notify("Transcribing…")
        dg = transcribe(audio, cfg)
        dg_path.write_text(json.dumps(dg))
    lines = build_transcript(dg, cfg)
    transcript = "\n\n".join(f"**[{_hms(s)}] {w}:** {t}" for s, w, t in lines) or "_No speech detected._"

    if notify:
        notify("Summarizing…")
    title = f"Call on {started:%B %-d}"
    try:
        summary = summarize(transcript, header.replace("  \n", "\n"), cfg) if lines else \
            f"# {title}\n\nNo speech was detected in this recording."
        m = re.match(r"#\s+(.+)", summary.strip())
        if m:
            title = m.group(1).strip()
    except Exception as e:  # model down or not set up: keep the transcript, say how to retry
        summary = (f"# {title}\n\nSummary unavailable: {e}\n\n"
                   f"Retry with `spitball reprocess \"{call_dir}\"`.")

    (call_dir / "transcript.md").write_text(f"# {title}: transcript\n\n{header}\n\n{transcript}\n")
    (call_dir / "summary.md").write_text(f"{summary.strip()}\n\n---\n\n{header}\n")

    # Rename the folder to include the title, once: 2026-09-28-1400-zoom -> …-zoom-weekly-sync
    final_dir = call_dir
    if not meta.get("titled"):
        final_dir = call_dir.with_name(f"{call_dir.name}-{_slug(title, 50)}")
        if not final_dir.exists():
            call_dir.rename(final_dir)
            meta["titled"] = True
            (final_dir / ".meta.json").write_text(json.dumps(meta))
        else:
            final_dir = call_dir

    if cfg.get("export_dir"):
        export = Path(cfg["export_dir"]).expanduser()
        export.mkdir(parents=True, exist_ok=True)
        (export / f"{final_dir.name}.md").write_text(
            f"{summary.strip()}\n\n---\n\n{header}  \n**Source:** Spitball, `{final_dir}`\n\n"
            f"## Transcript\n\n{transcript}\n")
    return {"dir": str(final_dir), "title": title, "summary": str(final_dir / "summary.md"),
            "ended_at": int(meta["started_at"] + meta["duration"])}


def audio_seconds(audio: Path) -> float:
    """Length of an audio file per ffprobe; 0 if it can't be read."""
    import subprocess
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "default=nw=1:nk=1", str(audio)],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        return float(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def discard(call_dir: Path) -> None:
    shutil.rmtree(call_dir, ignore_errors=True)


def new_call_dir(cfg: dict, app: str) -> Path:
    stamp = time.strftime("%Y-%m-%d-%H%M")
    base = Path(cfg["calls_dir"]).expanduser() / f"{stamp}-{_slug(app or 'manual', 20)}"
    d, n = base, 2
    while d.exists():
        d = base.with_name(f"{base.name}-{n}")
        n += 1
    d.mkdir(parents=True)
    return d
