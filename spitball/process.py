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

from . import calendar, config, diarize, providers, speakers
from .providers.deepgram import DEEPGRAM_URL  # re-exported: some callers/tests reference it here

MAX_TRANSCRIPT_CHARS = 180_000  # about 3 hours of talk

LIVE_TRANSCRIPT_FILENAME = ".live.json"
LIVE_TRANSCRIPT_MAX_FAILED_FRACTION = 0.1  # more than this much of the call's spoken time failed -> re-transcribe
SUMMARY_SEPARATOR = "\n\n---\n\n"  # summary.md = <model text> SEPARATOR <header>


def _hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _slug(text: str, limit: int = 60) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:limit].rstrip("-") or "call"


# ------------------------------------------------------------------ transcription

def transcribe(audio: Path, cfg: dict, hints: dict | None = None) -> dict:
    """Dispatches to the configured transcription_provider (deepgram/openai/
    local) and returns the shared normalized shape -- see spitball/providers/
    __init__.py. A module-level name (not `providers.transcribe` called
    inline) so tests can `mock.patch("spitball.process.transcribe", ...)`.
    `hints` carries the invitee count for the local speaker split."""
    return providers.transcribe(audio, cfg, hints=hints)


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


def build_transcript(normalized: dict, cfg: dict, named: bool = True, for_model: bool = False) -> list:
    """[(start_seconds, speaker_label, text)] with consecutive lines merged.
    `normalized` is the shared provider shape: {"utterances": [...]}
    (see spitball/providers/__init__.py's module docstring). Far-side labels
    come from spitball/speakers.py: "Them" for one far voice, "Speaker N"
    for several (tiny voices folded into their neighbors), and, with
    `named`, whatever the `speakers` map resolved each one to. `for_model`
    is the copy a model reads: a hand-set name that is an email address
    renders as the bare label there."""
    utts = sorted(normalized.get("utterances", []), key=lambda u: u["start"])
    utts = _drop_echo([u for u in utts if u.get("transcript", "").strip()])
    _, fold, labels = speakers.labels_for(normalized, cfg, named, for_model)
    lines = []
    for u in utts:
        who = cfg["my_name"] if u["channel"] == 0 else labels.get(speakers.folded_speaker(u, fold), "Them")
        text = u["transcript"].strip()
        if lines and lines[-1][1] == who and u["start"] - lines[-1][3] < 4:
            s, w, t, _ = lines[-1]
            lines[-1] = (s, w, f"{t} {text}", u["end"])
        else:
            lines.append((u["start"], who, text, u["end"]))
    return [(s, w, t) for s, w, t, _ in lines]


def _render_lines(lines: list) -> str:
    return "\n\n".join(f"**[{_hms(s)}] {w}:** {t}" for s, w, t in lines) or "_No speech detected._"


# ------------------------------------------------------------------ summary

SUMMARY_SYSTEM = """You write meeting notes from a call transcript for {me}.
Plain, direct American English. No filler, no hype, no preamble. Write the
summary in English unless the transcript itself is in another language, in
which case write the summary in that language instead.

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
labeled "{me}". If the notes above the transcript list the people on the
invite, those are the likely names of the other speakers: use them when the
transcript supports it, and never assume everyone invited was on the call.
A label like "Speaker 2 (probably Priya)" is an uncertain guess: keep that
exact wording wherever you refer to that speaker, never just the name.
Do not invent facts, names, or dates that are not in the transcript."""


def _post_json(url: str, body: dict | None, key: str, timeout: int) -> dict:
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def chat_completion(system: str, user: str, cfg: dict | None = None, max_tokens: int = 4000,
                    temperature: float = 0.3) -> str:
    """One chat completion against the configured summary endpoint (any
    OpenAI-compatible server: Ollama, LM Studio, OpenAI, OpenRouter…) --
    the summary and the speaker-naming pass both go through here. Returns
    the reply text with any <think> block removed; raises RuntimeError on
    any failure."""
    cfg = cfg or config.load()
    base = cfg["summary_base_url"].rstrip("/")
    key = config.secret(cfg, "summary_api_key")
    try:
        model = cfg.get("summary_model") or _post_json(f"{base}/models", None, key, 10)["data"][0]["id"]
        reply = _post_json(f"{base}/chat/completions", {
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
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


def summarize(transcript_text: str, meta: str, cfg: dict | None = None) -> str:
    """The call's summary: one chat completion. Raises on any failure; the
    caller keeps the transcript."""
    cfg = cfg or config.load()
    if not cfg.get("summary_enabled", True):
        raise RuntimeError("summaries are turned off (summary_enabled)")
    body = transcript_text
    if len(body) > MAX_TRANSCRIPT_CHARS:
        body = body[:MAX_TRANSCRIPT_CHARS] + "\n\n[transcript truncated]"
    me = cfg.get("my_name") or "Me"
    return chat_completion(SUMMARY_SYSTEM.replace("{me}", me), f"{meta}\n\nTranscript:\n\n{body}", cfg)


def check_summary(cfg: dict | None = None) -> dict:
    """`spitball check summary --json`: {"ok", "message", "models": [...]}
    from {summary_base_url}/models -- a real network call, for the Settings
    panel's Test button (and to fill its model dropdown)."""
    cfg = cfg or config.load()
    base = cfg["summary_base_url"].rstrip("/")
    key = config.secret(cfg, "summary_api_key")
    try:
        reply = _post_json(f"{base}/models", None, key, 10)
        models = [m["id"] for m in reply.get("data", []) if m.get("id")]
        return {"ok": True, "message": f"Reached {base}", "models": models}
    except urllib.error.HTTPError as e:
        return {"ok": False, "message": f"{base} error {e.code}: {e.read()[:200].decode(errors='replace')}",
                "models": []}
    except (urllib.error.URLError, TimeoutError) as e:
        return {"ok": False, "message": f"{base} unreachable ({getattr(e, 'reason', e)})", "models": []}
    except (KeyError, ValueError) as e:
        return {"ok": False, "message": f"{base} returned an unexpected reply ({e})", "models": []}


# ------------------------------------------------------------------ pipeline

def _load_cached_transcript(call_dir: Path, cfg: dict) -> dict | None:
    """The current cache is `.transcript.json` (the normalized shape, any
    provider). Folders made before this feature only have `.deepgram.json`
    (Deepgram's raw shape) -- read and normalize it on the fly so old call
    folders keep reprocessing without a re-transcribe."""
    tpath = call_dir / ".transcript.json"
    if tpath.exists():
        return json.loads(tpath.read_text())
    dg_path = call_dir / ".deepgram.json"
    if dg_path.exists():
        from .providers import deepgram
        normalized = deepgram.normalize(json.loads(dg_path.read_text()), cfg)
        tpath.write_text(json.dumps(normalized))  # migrate once; future reprocesses skip the old file
        return normalized
    return None


def _load_live_transcript(call_dir: Path, cfg: dict) -> dict | None:
    """`.live.json`, written by the live transcriber thread (spitball/live.py)
    when a recording stops -- reused as the transcript instead of
    transcribing the whole file again (spec: docs/SPEC-live-transcript.md).

    Only for the local provider: the live thread always transcribes with
    `local` regardless of `transcription_provider` (never deepgram, so it
    never costs money or sends audio anywhere mid-call), so reusing it under
    `deepgram` would silently downgrade a call the user asked to have
    transcribed in the cloud -- deepgram always does its own full
    transcription, same as before this feature existed.

    Falls back to None (a normal full transcription) when the file is
    missing/unparsable, has no utterances at all, or when failed segments
    cover more than LIVE_TRANSCRIPT_MAX_FAILED_FRACTION of the call's total
    spoken time -- a badly-degraded live pass (voxtype fell over repeatedly)
    isn't worth keeping over a clean, complete transcription."""
    if cfg.get("transcription_provider", "local") != "local":
        return None
    try:
        data = json.loads((call_dir / LIVE_TRANSCRIPT_FILENAME).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    utts = data.get("utterances")
    if not isinstance(utts, list) or not utts:
        return None
    total = sum(max(0.0, u.get("end", 0) - u.get("start", 0)) for u in utts)
    failed = sum(max(0.0, u.get("end", 0) - u.get("start", 0)) for u in utts if u.get("failed"))
    if total <= 0 or failed > LIVE_TRANSCRIPT_MAX_FAILED_FRACTION * total:
        return None
    return data


def _core_header(meta: dict) -> str:
    started = datetime.fromtimestamp(meta["started_at"])
    return (f"**Date:** {started:%B %-d, %Y, %-I:%M %p}  \n"
            f"**App:** {meta.get('app') or 'Manual'}  \n"
            f"**Length:** {_hms(meta['duration'])}")


def _base_header(meta: dict, decision: dict | None) -> str:
    """The header of transcript.md / summary.md (local files): date, app,
    length, and the matched meeting with its attendee list."""
    header = _core_header(meta)
    cal_lines = calendar.header_lines(decision)
    if cal_lines:
        header += "  \n" + "  \n".join(cal_lines)
    return header


def _model_header(meta: dict, decision: dict | None, cfg: dict) -> str:
    """What the summary endpoint is told about the call besides the
    transcript. Built apart from the local header on purpose: the
    calendar's part is `calendar.summary_context()` -- the meeting title and
    time, the invite list only when `calendar_names_to_summary`, the
    description only when `calendar_description_to_summary` -- so
    transcript.md's own `**Attendees:**` line never rides along."""
    header = _core_header(meta).replace("  \n", "\n")
    context = calendar.summary_context(decision, cfg)
    if context:
        header += "\n" + context
    return header


def _transcript_notes(normalized: dict) -> list:
    notes = []
    if normalized.get("note"):  # e.g. the local provider's Whisper-while-Parakeet-downloads fallback
        notes.append(f"**Note:** {normalized['note']}")
    failed = sum(1 for u in normalized.get("utterances", []) if u.get("failed"))
    if failed:
        notes.append(f"**Note:** {failed} part{'s' if failed != 1 else ''} of the audio couldn't be "
                     "transcribed and are marked in the text.")
    return notes


def _speaker_lines(normalized: dict, cfg: dict, naming_error: str = "", for_model: bool = False) -> list:
    """The `**Speakers:**` line, the invite-count sanity check, a note when
    the naming pass was attempted and failed, and a note for any hand-set
    name a re-transcription could not carry over."""
    order, _ = speakers.far_speaker_order(normalized.get("utterances", []), speakers.max_speakers(cfg))
    lines = speakers.summary_lines(normalized.get("speakers") or {}, single=len(order) == 1, for_model=for_model)
    mismatch = speakers.mismatch_line(diarize.expected_far_speakers(normalized.get("meeting")), len(order))
    if mismatch:
        lines.append(mismatch)
    if naming_error:
        lines.append(f"**Note:** speaker names unavailable: {naming_error}")
    dropped = speakers.dropped_line(normalized.get("speakers_dropped"))
    if dropped:
        lines.append(dropped)
    return lines


def _write_outputs(final_dir: Path, title: str, header: str, transcript: str, summary: str, cfg: dict) -> None:
    (final_dir / "transcript.md").write_text(f"# {title}: transcript\n\n{header}\n\n{transcript}\n")
    (final_dir / "summary.md").write_text(f"{summary.strip()}{SUMMARY_SEPARATOR}{header}\n")
    if cfg.get("export_dir"):
        export = Path(cfg["export_dir"]).expanduser()
        export.mkdir(parents=True, exist_ok=True)
        (export / f"{final_dir.name}.md").write_text(
            f"{summary.strip()}{SUMMARY_SEPARATOR}{header}  \n**Source:** Spitball, `{final_dir}`\n\n"
            f"## Transcript\n\n{transcript}\n")


def process(call_dir: Path, meta: dict, cfg: dict | None = None, notify=None,
            retranscribe: bool = False) -> dict:
    """Transcribe + summarize one call folder. Returns {dir, title, summary}.
    `meta` holds app/started_at/duration (also saved as meta.json for reprocessing).
    `retranscribe=True` (spitball reprocess --retranscribe) ignores any cached
    transcript and calls the provider again."""
    cfg = cfg or config.load()
    audio = call_dir / "audio.opus"
    meta_path = call_dir / ".meta.json"
    if meta:
        meta_path.write_text(json.dumps(meta))
    else:
        meta = json.loads(meta_path.read_text())

    started = datetime.fromtimestamp(meta["started_at"])

    # Calendar (spitball/calendar.py): the daemon snapshots the candidate
    # events into .meta.json at record start; the decision is made here,
    # once the duration is known. Never raises; None when the calendar is off.
    decision = calendar.for_call(meta, cfg, meta["duration"])
    meta_path.write_text(json.dumps(meta))  # the snapshot/decision travels with the folder
    event = decision["event"] if decision else None
    header = _base_header(meta, decision)
    use_event_title = bool(event) and bool(cfg.get("calendar_prefer_event_title", True))
    # `meeting` (CONTRACT.md "Transcript cache"): the matched event's title,
    # time, and attendees. Speaker naming reads `attendees`; the local
    # speaker split gets the invitee count as a hint. Rewritten on every run
    # so a changed match is reflected.
    meeting = calendar.meeting_record(decision)
    expected_far = diarize.expected_far_speakers(meeting)

    # The old cache is read even on a re-transcription: its hand-set
    # speaker names (`spitball speakers`) are carried onto the fresh result.
    previous = _load_cached_transcript(call_dir, cfg)
    cached = None if retranscribe else previous
    if cached is not None:
        normalized = cached
    else:
        # `reprocess --retranscribe` ignores .live.json too, same as it
        # ignores .transcript.json/.deepgram.json above.
        normalized = None if retranscribe else _load_live_transcript(call_dir, cfg)
        if normalized is None:
            if notify:
                notify("Transcribing…")
            normalized = transcribe(audio, cfg, hints={"far_speakers": expected_far})
        else:
            # The live transcript was made without a speaker split; split
            # it now (a no-op unless the add-on is installed and the call
            # had more than one other person).
            from .providers import local as local_provider
            normalized = diarize.split_transcript(audio, normalized, cfg, expected_far,
                                                  transcribe_piece=local_provider.transcribe_piece)
        if previous is not None:
            # A fresh transcript replaces the cache; the user's own names
            # follow their voices by provider id, and any that can't are
            # said out loud rather than lost.
            dropped = speakers.carry_user_names(previous, normalized, cfg)
            if dropped and notify:
                notify(speakers.dropped_line(dropped).replace("**Note:** ", ""))
    if meeting:
        normalized["meeting"] = meeting
    else:
        normalized.pop("meeting", None)
    (call_dir / ".transcript.json").write_text(json.dumps(normalized))

    # Speaker naming (spitball/speakers.py): who each far-side voice is,
    # from the invitees' names and what people say. Reads the transcript
    # with neutral labels; user renames from an earlier run survive.
    neutral = _render_lines(build_transcript(normalized, cfg, named=False))
    naming = speakers.resolve(normalized, cfg, neutral)
    (call_dir / ".transcript.json").write_text(json.dumps(normalized))

    notes = _transcript_notes(normalized) + _speaker_lines(normalized, cfg, naming["error"])
    if notes:
        header += "  \n" + "  \n".join(notes)
    lines = build_transcript(normalized, cfg)
    transcript = _render_lines(lines)

    if notify:
        notify("Summarizing…")
    title = event["title"] if use_event_title else f"Call on {started:%B %-d}"
    # The summarizer's metadata is NOT the local header: the calendar part
    # of it obeys the Calendar settings (see _model_header). The notes ride
    # along -- a `**Speakers:**` line only carries names the naming pass
    # (`speaker_names`) or the user already put on the transcript itself.
    # Everything model-bound is rendered as its own copy and scrubbed last
    # (calendar.scrub_for_model): no email address, no feed address, ever.
    model_notes = _transcript_notes(normalized) + _speaker_lines(normalized, cfg, naming["error"], for_model=True)
    summary_meta = _model_header(meta, decision, cfg)
    if model_notes:
        summary_meta += "\n" + "\n".join(model_notes)
    summary_meta = calendar.scrub_for_model(summary_meta, cfg)
    model_transcript = _render_lines(build_transcript(normalized, cfg, for_model=True))
    try:
        summary = summarize(model_transcript, summary_meta, cfg) if lines else \
            f"# {title}\n\nNo speech was detected in this recording."
        m = re.match(r"#\s+(.+)", summary.strip())
        if use_event_title:
            # The event's title wins everywhere (folder, transcript, summary)
            # so the three agree; the model's own heading is dropped.
            body = summary.strip()[m.end():].lstrip("\n") if m else summary.strip()
            summary = f"# {title}\n\n{body}"
        elif m:
            title = m.group(1).strip()
    except Exception as e:  # model down or not set up: keep the transcript, say how to retry
        summary = (f"# {title}\n\nSummary unavailable: {e}\n\n"
                   f"Retry with `spitball reprocess \"{call_dir}\"`.")

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

    _write_outputs(final_dir, title, header, transcript, summary, cfg)
    return {"dir": str(final_dir), "title": title, "summary": str(final_dir / "summary.md"),
            "ended_at": int(meta["started_at"] + meta["duration"])}


# ------------------------------------------------------------------ speakers CLI

def _read_summary(call_dir: Path, fallback_title: str) -> tuple:
    """(title, model text) from an existing summary.md, without its header."""
    try:
        text = (call_dir / "summary.md").read_text()
    except OSError:
        return fallback_title, f"# {fallback_title}\n\nSummary unavailable."
    body = text.rsplit(SUMMARY_SEPARATOR, 1)[0].strip() if SUMMARY_SEPARATOR in text else text.strip()
    m = re.match(r"#\s+(.+)", body)
    return (m.group(1).strip() if m else fallback_title), body


def rerender(call_dir: Path, cfg: dict | None = None, label_change: tuple | None = None) -> dict:
    """Rewrites transcript.md, summary.md, and the export copy from the
    cached transcript and the existing summary text -- no transcription,
    no model call. `label_change` is ({n: old label}, {n: new label}) from
    a rename, applied to the summary's own wording. Used by `spitball
    speakers`; `spitball reprocess <dir>` is the way to get a fresh summary."""
    cfg = cfg or config.load()
    meta = json.loads((call_dir / ".meta.json").read_text())
    normalized = _load_cached_transcript(call_dir, cfg)
    if normalized is None:
        raise RuntimeError(f"no cached transcript in {call_dir}")
    decision = calendar.for_call(meta, cfg, meta.get("duration"))
    header = _base_header(meta, decision)
    notes = _transcript_notes(normalized) + _speaker_lines(normalized, cfg)
    if notes:
        header += "  \n" + "  \n".join(notes)
    transcript = _render_lines(build_transcript(normalized, cfg))
    started = datetime.fromtimestamp(meta["started_at"])
    title, summary = _read_summary(call_dir, f"Call on {started:%B %-d}")
    if label_change:
        summary = speakers.rename_in_summary(summary, *label_change)
    _write_outputs(call_dir, title, header, transcript, summary, cfg)
    return {"dir": str(call_dir), "title": title, "summary": str(call_dir / "summary.md")}


def list_speakers(call_dir: Path, cfg: dict | None = None) -> list:
    """`spitball speakers <dir>`: the far-side speakers and their names."""
    cfg = cfg or config.load()
    normalized = _load_cached_transcript(call_dir, cfg)
    if normalized is None:
        raise RuntimeError(f"no cached transcript in {call_dir} (run `spitball reprocess` first)")
    return speakers.listing(normalized, cfg)


def rename_speaker(call_dir: Path, n: int, name: str, cfg: dict | None = None) -> list:
    """`spitball speakers <dir> <n> "Name"` (or "" to clear): records the
    user's answer in .transcript.json and re-renders the three outputs.
    Returns the new listing. Raises ValueError for a bad speaker number."""
    cfg = cfg or config.load()
    normalized = _load_cached_transcript(call_dir, cfg)
    if normalized is None:
        raise RuntimeError(f"no cached transcript in {call_dir} (run `spitball reprocess` first)")
    old = _label_map(normalized, cfg)
    speakers.set_name(normalized, cfg, n, name)
    new = _label_map(normalized, cfg)
    (call_dir / ".transcript.json").write_text(json.dumps(normalized))
    rerender(call_dir, cfg, label_change=(old, new))
    return speakers.listing(normalized, cfg)


def _label_map(normalized: dict, cfg: dict) -> dict:
    order, _, labels = speakers.labels_for(normalized, cfg)
    return {str(n): labels[spk] for n, spk in enumerate(order, 1)}


def set_calendar_override(call_dir: Path, event_id: str | None) -> None:
    """`spitball reprocess <dir> --event <id>` / `--no-event`: pin the
    calendar match to one of the snapshot's candidates (by id, as `spitball
    calendar test --json` and .meta.json list them) or to none. Stored in
    .meta.json under calendar.override and honored by every later run."""
    meta_path = call_dir / ".meta.json"
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, ValueError):
        meta = {}
    cal = meta.get("calendar")
    if not isinstance(cal, dict):
        cal = meta["calendar"] = {}
    cal["override"] = {"event": event_id}
    meta_path.write_text(json.dumps(meta))


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
