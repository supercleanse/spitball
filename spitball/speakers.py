"""Who the far-side speakers are (docs/SPEC-v2.md section 4).

A provider labels the far channel with speaker ids (Deepgram's own
diarization, or the local split in spitball/diarize.py); this module turns
those ids into people. Three sources, in order of authority:

  user      `spitball speakers <dir> <n> "Name"` -- never overwritten.
  calendar  a 1:1 call: exactly one other invitee and exactly one far
            voice, so the far side is that person, no model needed.
  llm       one short call to the summary endpoint with the transcript
            and the invite list, asking which "Speaker N" is which invitee
            and why ("0:42 'thanks, Priya' right after her turn"). Strict
            JSON back; a name that isn't on the invite is thrown away, a
            name claimed for two speakers makes both uncertain, and a
            claim without evidence is downgraded.

Rendering keeps the uncertainty visible: a high-confidence match reads as
the name, a medium one as "Speaker 2 (probably Priya Nair)", anything
weaker stays "Speaker 2" ("Them" when there is only one far voice). The
map lives in .transcript.json under `speakers` (CONTRACT.md "Transcript
cache"), keyed by the label number, so transcript.md, summary.md, and the
export copy are re-rendered from it rather than edited in place.

Before any of that, far-side ids are folded: a voice with only a few
seconds and a handful of words (an "mm-hm" the segmenter split off, a
notification sound Deepgram gave its own id) goes to the voice speaking
around it, and voices beyond `speaker_max` fold the same way. Folding is
applied at render time and never rewrites the provider's utterances.

No pitch or gender inference, no voiceprints: names come only from what
people say and who was invited.
"""
from __future__ import annotations

import json
import re

from . import config

CONFIDENCES = ("high", "medium", "low", "none")
SOURCES = ("user", "calendar", "llm", "")
# A far-side id with less talk than BOTH of these folds into its neighbor.
FOLD_MAX_S = 5.0
FOLD_MAX_WORDS = 12
NAMING_MAX_CHARS = 60_000  # the naming pass reads the start of a long call, where introductions happen

NAMING_SYSTEM = """You identify who the unnamed speakers in a call transcript are.

The transcript labels the user as "{me}" (that side is certain) and everyone
else as "Speaker 1", "Speaker 2", and so on. You are given the list of people
who were invited to the meeting. Match each "Speaker N" to one invitee ONLY
when the transcript itself shows it:
- someone introduces themselves ("this is Priya", "Alex here")
- someone is addressed by name right before or after their turn ("thanks,
  Priya" said by the next speaker means the previous speaker was Priya;
  "go ahead, Alex" means the next speaker is Alex)
- "{me}" addresses a speaker by name
- first-person facts that only one invitee fits (their team, their role)

Rules: one name per speaker and one speaker per name; never use a name that
is not on the invite list; never guess from tone, topic, or gender; when
nothing in the transcript supports a match, answer null for that speaker.
Confidence: "high" only for a direct introduction or a clear address by
name; "medium" for a reasonable inference; "low" when it is a guess.

Reply with JSON only, no prose, in exactly this shape:
{"Speaker 1": {"name": "<invitee name exactly as listed>", "confidence": "high|medium|low", "evidence": "<timestamp and the words that show it>"}, "Speaker 2": null}"""


# ------------------------------------------------------------------ ids -> labels

def _words(text: str) -> int:
    return len((text or "").split())


def far_speaker_order(utterances: list, max_speakers: int | None = None) -> tuple:
    """(ordered far-side ids, fold map {id: id it folds into}). Ids are
    ordered by first appearance so "Speaker 1" is whoever spoke first;
    tiny ids and ids beyond `max_speakers` fold into the id speaking
    nearest to them in time (the previous far utterance's id, else the
    next one's)."""
    far = sorted((u for u in utterances if u.get("channel") == 1 and (u.get("transcript") or "").strip()
                  and not u.get("failed")), key=lambda u: u["start"])
    talk: dict = {}
    words: dict = {}
    order: list = []
    for u in far:
        spk = u.get("speaker", 0)
        if spk not in talk:
            talk[spk] = 0.0
            words[spk] = 0
            order.append(spk)
        talk[spk] += max(0.0, u["end"] - u["start"])
        words[spk] += _words(u["transcript"])
    if len(order) <= 1:
        return order, {}
    tiny = {spk for spk in order if talk[spk] < FOLD_MAX_S and words[spk] < FOLD_MAX_WORDS}
    keep = [spk for spk in order if spk not in tiny]
    if not keep:  # nobody said much (a short call): there is no bigger voice to fold into
        keep = list(order)
    if max_speakers and max_speakers >= 1 and len(keep) > max_speakers:
        ranked = sorted(keep, key=lambda k: (-talk[k], order.index(k)))
        keep = [spk for spk in order if spk in set(ranked[:max_speakers])]
    keep_set = set(keep)
    fold: dict = {}
    for i, u in enumerate(far):
        spk = u.get("speaker", 0)
        if spk in keep_set or spk in fold:
            continue
        target = None
        for j in range(i - 1, -1, -1):
            if far[j].get("speaker", 0) in keep_set:
                target = far[j]["speaker"]
                break
        if target is None:
            for j in range(i + 1, len(far)):
                if far[j].get("speaker", 0) in keep_set:
                    target = far[j]["speaker"]
                    break
        fold[spk] = keep[0] if target is None else target
    return keep, fold


def folded_speaker(u: dict, fold: dict) -> int:
    spk = u.get("speaker", 0)
    return fold.get(spk, spk)


def max_speakers(cfg: dict) -> int:
    try:
        n = int(cfg.get("speaker_max", config.DEFAULTS["speaker_max"]))
    except (TypeError, ValueError):
        n = config.DEFAULTS["speaker_max"]
    return max(1, min(12, n))


# ------------------------------------------------------------------ the map

def empty_entry(spk_id: int) -> dict:
    return {"id": spk_id, "name": "", "confidence": "none", "source": "", "evidence": ""}


def normalize_map(raw, order: list) -> dict:
    """The `speakers` block for the current far-side ids: one entry per
    label number ("1".."N"), carrying over any cached entry for the same
    provider id (user renames survive a reprocess), dropping entries whose
    id is gone."""
    cached: dict = {}
    if isinstance(raw, dict):
        for entry in raw.values():
            if isinstance(entry, dict) and "id" in entry:
                try:
                    cached[int(entry["id"])] = entry
                except (TypeError, ValueError):
                    continue
    out = {}
    for n, spk in enumerate(order, 1):
        entry = dict(cached.get(spk) or empty_entry(spk))
        entry["id"] = spk
        entry["name"] = str(entry.get("name") or "").strip()
        entry["confidence"] = entry.get("confidence") if entry.get("confidence") in CONFIDENCES else "none"
        entry["source"] = entry.get("source") if entry.get("source") in SOURCES else ""
        entry["evidence"] = str(entry.get("evidence") or "")
        if not entry["name"]:
            entry["confidence"], entry["source"], entry["evidence"] = "none", "", ""
        out[str(n)] = entry
    return out


def label(n: int, entry: dict | None, single: bool, named: bool = True) -> str:
    """The rendered label for far speaker number `n`."""
    base = "Them" if single else f"Speaker {n}"
    if not named or not entry or not entry.get("name"):
        return base
    if entry.get("source") == "user" or entry.get("confidence") == "high":
        return entry["name"]
    if entry.get("confidence") == "medium":
        return f"{base} (probably {entry['name']})"
    return base


def labels_for(normalized: dict, cfg: dict, named: bool = True) -> tuple:
    """(order, fold, {far id: rendered label}) for build_transcript."""
    order, fold = far_speaker_order(normalized.get("utterances", []), max_speakers(cfg))
    speakers = normalize_map(normalized.get("speakers"), order) if named else {}
    single = len(order) == 1
    out = {}
    for n, spk in enumerate(order, 1):
        out[spk] = label(n, speakers.get(str(n)), single, named)
    return order, fold, out


def summary_lines(speakers: dict, single: bool) -> list:
    """`**Speakers:**` for the transcript/summary header: every far label
    and what it resolved to. Empty when nothing is named (the bare labels
    already say all there is to say)."""
    if not speakers or not any(e.get("name") for e in speakers.values()):
        return []
    parts = []
    for n in sorted(speakers, key=int):
        e = speakers[n]
        text = label(int(n), e, single)
        base = "Them" if single else f"Speaker {n}"
        if text == base and e.get("name"):
            text = f"{base} (unsure: {e['name']}?)"
        parts.append(text if text == base else f"{base} = {text}" if not text.startswith(base) else text)
    return [f"**Speakers:** {', '.join(parts)}"]


def mismatch_line(expected: int | None, found: int) -> str:
    """The sanity check that explains most bad splits: a shared room, hold
    music, a late joiner who wasn't on the invite."""
    if not expected or not found or expected == found:
        return ""
    return (f"**Note:** the invite lists {expected} other {'person' if expected == 1 else 'people'}; "
            f"{found} {'voice was' if found == 1 else 'voices were'} found on the far side.")


# ------------------------------------------------------------------ candidates

def candidates(meeting: dict | None) -> list:
    """The far-side invitees: everyone who isn't `self` and didn't decline,
    as {"name", "email"} with a display name (the address when the invite
    carries no name)."""
    if not meeting or not isinstance(meeting.get("attendees"), list):
        return []
    out = []
    seen = set()
    for a in meeting["attendees"]:
        if not isinstance(a, dict) or a.get("self") or a.get("response") == "declined":
            continue
        name = (a.get("name") or "").strip() or (a.get("email") or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append({"name": name, "email": (a.get("email") or "").strip()})
    return out


def _match_candidate(name: str, cands: list) -> str:
    """The candidate's display name for what the model wrote: exact
    (case-insensitive) first, then a unique first-name or address match."""
    key = (name or "").strip().lower()
    if not key:
        return ""
    for c in cands:
        if c["name"].lower() == key or (c["email"] and c["email"].lower() == key):
            return c["name"]
    hits = [c for c in cands if c["name"].lower().split()[0] == key.split()[0]
            or (c["email"] and c["email"].lower().split("@")[0] == key)]
    return hits[0]["name"] if len(hits) == 1 else ""


# ------------------------------------------------------------------ resolving

def resolve(normalized: dict, cfg: dict, transcript_text: str, chat=None) -> dict:
    """Fills `normalized["speakers"]` for the current far-side ids and
    returns {"speakers", "method": "user"|"calendar"|"llm"|"none",
    "error"}. User entries are kept as they are; everything else is
    recomputed. `transcript_text` is the transcript rendered with neutral
    labels ("Speaker N" / "Them"). Never raises."""
    order, _ = far_speaker_order(normalized.get("utterances", []), max_speakers(cfg))
    speakers = normalize_map(normalized.get("speakers"), order)
    report = {"speakers": speakers, "method": "none", "error": ""}
    if not order:
        normalized.pop("speakers", None)
        return report
    normalized["speakers"] = speakers
    cands = candidates(normalized.get("meeting"))
    todo = {n: e for n, e in speakers.items() if e.get("source") != "user"}
    if not todo:
        report["method"] = "user"
        return report
    for e in todo.values():  # clear stale automatic answers before recomputing
        e.update(name="", confidence="none", source="", evidence="")
    if not cfg.get("speaker_names", True) or not cands:
        return report
    if len(cands) == 1 and len(order) == 1 and "1" in todo:
        todo["1"].update(name=cands[0]["name"], confidence="high", source="calendar",
                         evidence="the only other person on the invite")
        report["method"] = "calendar"
        return report
    if not cfg.get("summary_enabled", True):
        report["error"] = "summaries are turned off (summary_enabled)"
        return report
    try:
        answers = name_with_llm(transcript_text, cands, cfg.get("my_name") or "Me", cfg, chat=chat)
    except RuntimeError as e:
        report["error"] = str(e)
        return report
    report["method"] = "llm"
    for n, e in todo.items():
        a = answers.get(n)
        if a:
            e.update(name=a["name"], confidence=a["confidence"], source="llm", evidence=a["evidence"])
    return report


def _chat(system: str, user: str, cfg: dict, max_tokens: int) -> str:
    from . import process
    return process.chat_completion(system, user, cfg, max_tokens=max_tokens, temperature=0.0)


def name_with_llm(transcript_text: str, cands: list, me: str, cfg: dict, chat=None) -> dict:
    """One naming call. Returns {"1": {"name", "confidence", "evidence"},
    ...} for the speakers the model could place (validated against the
    invite list). Raises RuntimeError when the endpoint fails."""
    chat = chat or _chat
    body = transcript_text
    if len(body) > NAMING_MAX_CHARS:
        body = body[:NAMING_MAX_CHARS] + "\n\n[transcript truncated]"
    invite = "\n".join(f"- {c['name']}" + (f" <{c['email']}>" if c["email"] else "") for c in cands)
    user = f"People on the invite (besides {me}):\n{invite}\n\nTranscript:\n\n{body}"
    reply = chat(NAMING_SYSTEM.replace("{me}", me), user, cfg, 800)
    return parse_answers(reply, cands)


def parse_answers(reply: str, cands: list) -> dict:
    """The model's JSON -> validated answers keyed by label number."""
    text = re.sub(r"<think>.*?</think>", "", reply or "", flags=re.S)
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        raise RuntimeError("naming model returned no JSON")
    try:
        data = json.loads(m.group(0))
    except ValueError:
        raise RuntimeError("naming model returned unparsable JSON")
    if not isinstance(data, dict):
        raise RuntimeError("naming model returned the wrong shape")
    answers: dict = {}
    for key, value in data.items():
        km = re.match(r"^\s*(?:speaker\s*)?(\d+)\s*$", str(key), flags=re.I)
        if not km or not isinstance(value, dict):
            continue
        name = _match_candidate(str(value.get("name") or ""), cands)
        if not name:
            continue
        confidence = str(value.get("confidence") or "").strip().lower()
        if confidence not in ("high", "medium", "low"):
            confidence = "low"
        evidence = str(value.get("evidence") or "").strip()
        if not evidence and confidence == "high":
            confidence = "medium"
        answers[km.group(1)] = {"name": name, "confidence": confidence, "evidence": evidence}
    # One speaker per name: a name claimed twice makes every claim uncertain.
    by_name: dict = {}
    for n, a in answers.items():
        by_name.setdefault(a["name"].lower(), []).append(n)
    for ns in by_name.values():
        if len(ns) > 1:
            for n in ns:
                answers[n]["confidence"] = "low"
    return {n: a for n, a in answers.items() if a["confidence"] != "low"}


# ------------------------------------------------------------------ CLI helpers

def set_name(normalized: dict, cfg: dict, n: int, name: str) -> dict:
    """`spitball speakers <dir> <n> "Name"` / `--clear`: a user entry
    (kept forever) or, with an empty name, back to automatic. Returns the
    updated map. Raises ValueError for an unknown speaker number."""
    order, _ = far_speaker_order(normalized.get("utterances", []), max_speakers(cfg))
    speakers = normalize_map(normalized.get("speakers"), order)
    key = str(n)
    if key not in speakers:
        raise ValueError(f"no far-side speaker {n} (this call has {len(order)})")
    name = (name or "").strip()
    if name:
        speakers[key].update(name=name, confidence="high", source="user", evidence="set by hand")
    else:
        speakers[key].update(name="", confidence="none", source="", evidence="")
    normalized["speakers"] = speakers
    return speakers


def listing(normalized: dict, cfg: dict) -> list:
    """Rows for `spitball speakers <dir>`: [{"n", "label", "id", "name",
    "confidence", "source", "evidence", "seconds", "words"}]."""
    utts = normalized.get("utterances", [])
    order, fold = far_speaker_order(utts, max_speakers(cfg))
    speakers = normalize_map(normalized.get("speakers"), order)
    single = len(order) == 1
    rows = []
    for n, spk in enumerate(order, 1):
        e = speakers[str(n)]
        mine = [u for u in utts if u.get("channel") == 1 and folded_speaker(u, fold) == spk
                and not u.get("failed")]
        rows.append({"n": n, "label": label(n, e, single), "id": spk, "name": e["name"],
                     "confidence": e["confidence"], "source": e["source"], "evidence": e["evidence"],
                     "seconds": round(sum(max(0.0, u["end"] - u["start"]) for u in mine), 1),
                     "words": sum(_words(u.get("transcript")) for u in mine)})
    return rows


def format_listing(rows: list) -> str:
    if not rows:
        return "No far-side speech in this call."
    lines = []
    for r in rows:
        base = "Them" if len(rows) == 1 else f"Speaker {r['n']}"
        if r["name"]:
            how = f"{r['source']}, {r['confidence']}" if r["source"] else r["confidence"]
            tail = f"{r['name']} ({how})"
        else:
            tail = "(unnamed)"
        line = f"{r['n']}  {base:<10} {tail}  [{r['words']} words, {r['seconds']:.0f}s]"
        if r["evidence"] and r["source"] != "user":
            line += f"\n   {r['evidence']}"
        lines.append(line)
    return "\n".join(lines)


def rename_in_summary(text: str, old_labels: dict, new_labels: dict) -> str:
    """Rewrites speaker labels inside an existing summary body after a
    rename, so `spitball speakers` needn't call the model again: every old
    rendering of a label ("Speaker 2", "Speaker 2 (probably X)", the old
    name) becomes the new one. Longest matches first so "Speaker 12" isn't
    hit by "Speaker 1"."""
    pairs = []
    for n, old in old_labels.items():
        new = new_labels.get(n)
        if new is None or new == old:
            continue
        base = "Them" if len(old_labels) == 1 else f"Speaker {n}"
        forms = {old, base}
        for form in forms:
            pairs.append((form, new))
    pairs.sort(key=lambda p: -len(p[0]))
    for old, new in pairs:
        text = re.sub(rf"(?<!\w){re.escape(old)}(?:\s*\(probably [^)]*\))?(?!\w)", new, text)
    return text
