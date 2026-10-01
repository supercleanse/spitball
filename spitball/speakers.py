"""Who the far-side speakers are (docs/SPEC-v2.md section 4).

A provider labels the far channel with speaker ids (Deepgram's own
diarization, or the local split in spitball/diarize.py); this module turns
those ids into people. Three sources, in order of authority:

  user      `spitball speakers <dir> <n> "Name"` -- never overwritten.
  calendar  a 1:1 call: exactly one other invitee and exactly one far
            voice, so the far side is that person, no model needed. An
            invitee listed by address only still gets a name here, from
            spitball/people.py (the name you gave that address before,
            the meeting title, or the address itself); only a name taken
            from a bare one-part address ("jordan@") renders as
            "Them (probably Jordan)".
  llm       one short call to the summary endpoint with the transcript
            and the invitees' names -- never their email addresses; an
            invitee with no name on the invite is sent as a local
            placeholder ("Invitee 2") that maps back here -- asking which
            "Speaker N" is which invitee and why ("0:42 'thanks, Priya'
            right after her turn"). Strict JSON back; a name that isn't on
            the invite is thrown away, a name claimed for two speakers
            makes both uncertain, and a claim without evidence is
            downgraded. This pass is governed by `speaker_names` alone:
            it cannot work without the names, so the Calendar page's
            `calendar_names_to_summary` (the summary request's own invite
            list) does not gate it.

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


def label(n: int, entry: dict | None, single: bool, named: bool = True, for_model: bool = False) -> str:
    """The rendered label for far speaker number `n`. `for_model`: the copy
    a model reads -- a name that is an email address (only possible by
    hand, `spitball speakers … "x@y"`) renders as the bare label there."""
    base = "Them" if single else f"Speaker {n}"
    if not named or not entry or not entry.get("name"):
        return base
    if for_model and "@" in entry["name"]:
        return base
    if entry.get("source") == "user" or entry.get("confidence") == "high":
        return entry["name"]
    if entry.get("confidence") == "medium":
        return f"{base} (probably {entry['name']})"
    return base


def labels_for(normalized: dict, cfg: dict, named: bool = True, for_model: bool = False) -> tuple:
    """(order, fold, {far id: rendered label}) for build_transcript."""
    order, fold = far_speaker_order(normalized.get("utterances", []), max_speakers(cfg))
    speakers = normalize_map(normalized.get("speakers"), order) if named else {}
    single = len(order) == 1
    out = {}
    for n, spk in enumerate(order, 1):
        out[spk] = label(n, speakers.get(str(n)), single, named, for_model)
    return order, fold, out


def summary_lines(speakers: dict, single: bool, for_model: bool = False) -> list:
    """`**Speakers:**` for the transcript/summary header: every far label
    and what it resolved to. Empty when nothing is named (the bare labels
    already say all there is to say)."""
    if for_model:
        speakers = {n: e for n, e in speakers.items() if "@" not in str(e.get("name") or "")}
    if not speakers or not any(e.get("name") for e in speakers.values()):
        return []
    parts = []
    for n in sorted(speakers, key=int):
        e = speakers[n]
        text = label(int(n), e, single, for_model=for_model)
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

def _far_invitees(meeting: dict | None) -> list:
    if not meeting or not isinstance(meeting.get("attendees"), list):
        return []
    return [a for a in meeting["attendees"]
            if isinstance(a, dict) and not a.get("self") and a.get("response") != "declined"]


def candidates(meeting: dict | None) -> list:
    """The far-side invitees a speaker can be named as: everyone who isn't
    `self` and didn't decline, AND has a name -- on the invite, or found
    by spitball/people.py for an invitee listed by address only (the name
    you gave that address before, a matching word in the meeting title,
    or the address's own parts) -- as {"name", "email", "source",
    "sure"}. A name is never an email address, because the names go into
    the transcript the summary model reads. Invitees with no name to be
    had are only counted (nameless_count) so the model knows the invite
    was bigger than the list it sees."""
    from . import people
    far = _far_invitees(meeting)
    title = str((meeting or {}).get("title") or "")
    peers = [(a.get("email") or "").strip() for a in far]
    book = people.load_book()
    out = []
    seen = set()
    for a in far:
        name, source, sure = people.display_name(a, title, peers, book)
        if not name or "@" in name or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append({"name": name, "email": (a.get("email") or "").strip(),
                    "source": source, "sure": sure})
    return out


def nameless_count(meeting: dict | None) -> int:
    """How many far-side invitees have no usable name, on the invite or
    from spitball/people.py."""
    from . import people
    far = _far_invitees(meeting)
    title = str((meeting or {}).get("title") or "")
    peers = [(a.get("email") or "").strip() for a in far]
    book = people.load_book()
    named = {c["name"].lower() for c in candidates(meeting)}
    n = 0
    seen = set()
    for a in far:
        name = people.display_name(a, title, peers, book)[0]
        if name and "@" not in name and name.lower() in named:
            continue
        key = (a.get("email") or a.get("name") or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        n += 1
    return n


def _match_candidate(name: str, cands: list) -> str:
    """The candidate's display name for what the model wrote: an exact
    (case-insensitive) match, then a unique first-name match. Never by
    email address -- the model was never given one, so matching on it
    would be a guess."""
    key = (name or "").strip().lower()
    if not key:
        return ""
    for c in cands:
        if c["name"].lower() == key:
            return c["name"]
    first = key.split()[0]
    hits = [c for c in cands if c["name"].lower().split()[0] == first]
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
    nameless = nameless_count(normalized.get("meeting"))
    if len(cands) == 1 and nameless == 0 and len(order) == 1 and "1" in todo:
        c = cands[0]
        how = {"book": "the name you gave this address before",
               "title": "name from the meeting title",
               "address": "name from their email address"}.get(c.get("source"))
        todo["1"].update(name=c["name"], confidence="high" if c.get("sure", True) else "medium",
                         source="calendar",
                         evidence="the only other person on the invite" + (f" ({how})" if how else ""))
        report["method"] = "calendar"
        return report
    if not cfg.get("summary_enabled", True):
        report["error"] = "summaries are turned off (summary_enabled)"
        return report
    try:
        answers = name_with_llm(transcript_text, cands, cfg.get("my_name") or "Me", cfg, chat=chat,
                                nameless=nameless)
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


def name_with_llm(transcript_text: str, cands: list, me: str, cfg: dict, chat=None,
                  nameless: int = 0) -> dict:
    """One naming call. Returns {"1": {"name", "confidence", "evidence"},
    ...} for the speakers the model could place (validated against the
    invite list). Raises RuntimeError when the endpoint fails."""
    from . import calendar as _calendar
    chat = chat or _chat
    body = transcript_text
    if len(body) > NAMING_MAX_CHARS:
        body = body[:NAMING_MAX_CHARS] + "\n\n[transcript truncated]"
    # Names only: the address never leaves the machine. Invitees with no
    # name are a count, so the model knows not everyone is on its list.
    invite = "\n".join(f"- {c['name']}" for c in cands)
    if nameless:
        invite += f"\n(and {nameless} more invitee{'s' if nameless != 1 else ''} with no name on the invite)"
    user = f"People on the invite (besides {me}):\n{invite}\n\nTranscript:\n\n{body}"
    user = _calendar.scrub_for_model(user, cfg)
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


# ------------------------------------------------------------------ re-transcription

def identity(normalized: dict | None) -> dict:
    """What a far-side speaker id MEANS in a transcript -- the frame the ids
    live in: {"provider", "split", "voices"}. Deepgram's ids come from its
    own diarization; the local provider's from the sherpa-onnx split
    (`diarization.ran`) or, unsplit, one id for everyone ("Them"). Two
    transcripts' ids are comparable only when all three agree; a Deepgram
    speaker 0 and an unsplit local speaker 0 are not the same person."""
    n = normalized or {}
    provider = str(n.get("provider") or "")
    diar = n.get("diarization") if isinstance(n.get("diarization"), dict) else None
    if provider == "deepgram":
        split = "deepgram"
    elif diar and diar.get("ran"):
        split = str(diar.get("engine") or "local-split")
    else:
        split = "none"
    voices = {u.get("speaker", 0) for u in n.get("utterances", []) or []
              if isinstance(u, dict) and u.get("channel") == 1 and (u.get("transcript") or "").strip()
              and not u.get("failed")}
    return {"provider": provider, "split": split, "voices": len(voices)}


def identity_mismatch(old: dict, new: dict) -> str:
    """"" when ids are comparable, else why not (one line, for the note)."""
    if old.get("provider") != new.get("provider"):
        return f"provider changed ({old.get('provider') or '?'} → {new.get('provider') or '?'})"
    if old.get("split") != new.get("split"):
        return f"speaker split changed ({old.get('split') or '?'} → {new.get('split') or '?'})"
    if old.get("voices") != new.get("voices"):
        return f"a different number of far-side voices ({old.get('voices')} → {new.get('voices')})"
    return ""


def carry_user_names(previous: dict | None, normalized: dict, cfg: dict) -> list:
    """After `reprocess --retranscribe`: the hand-set (`source: user`)
    entries of the old cache's `speakers` block are put onto the fresh
    transcript so normalize_map() can follow each one to its voice by
    provider id -- but only when the two transcripts' ids mean the same
    thing (identity(): same provider, same split, same voice count). When
    they don't (Deepgram -> local, a split that came or went, a different
    voice count), or an id simply isn't there any more, the entry can't be
    placed: it is recorded under `speakers_dropped` ([{"name", "id",
    "was", "reason"}]) and returned, so the header and the CLI say so
    instead of the name quietly disappearing -- or, worse, landing on
    someone else. Automatic (calendar/llm) entries are not carried;
    resolve() recomputes those anyway."""
    normalized.pop("speakers_dropped", None)
    block = (previous or {}).get("speakers")
    if not isinstance(block, dict) or not block:
        return []
    mismatch = identity_mismatch(identity(previous), identity(normalized))
    order, _ = far_speaker_order(normalized.get("utterances", []), max_speakers(cfg))
    ids = set(order)
    single = len(block) == 1
    carry: dict = {}
    dropped: list = []
    for n in sorted(block, key=lambda k: int(k) if str(k).isdigit() else 0):
        e = block[n]
        if not isinstance(e, dict) or e.get("source") != "user" or not str(e.get("name") or "").strip():
            continue
        try:
            spk = int(e["id"])
        except (KeyError, TypeError, ValueError):
            continue
        if not mismatch and spk in ids:
            carry[str(n)] = dict(e)
        else:
            dropped.append({"name": str(e["name"]).strip(), "id": spk,
                            "was": "Them" if single else f"Speaker {n}",
                            "reason": mismatch or "that voice is gone"})
    if carry:
        normalized["speakers"] = carry  # re-keyed by resolve()/normalize_map()
    if dropped:
        normalized["speakers_dropped"] = dropped
    return dropped


def dropped_line(dropped) -> str:
    """The header note for names a re-transcription could not carry over."""
    if not isinstance(dropped, list) or not dropped:
        return ""
    items = [d for d in dropped if isinstance(d, dict) and d.get("name")]
    if not items:
        return ""
    parts = ", ".join(f"{d['name']} (was {d.get('was') or 'a far-side speaker'})" for d in items)
    plural = len(items) != 1
    reasons = {str(d.get("reason") or "") for d in items} - {"", "that voice is gone"}
    why = f"re-transcribing changed the far-side voices ({', '.join(sorted(reasons))})" if reasons \
        else "re-transcribing changed the far-side voices"
    return (f"**Note:** {why}, so {len(items)} hand-set "
            f"name{'s' if plural else ''} could not be carried over: {parts}. "
            f"Set {'them' if plural else 'it'} again with `spitball speakers <call-dir> <n> \"Name\"`.")


# ------------------------------------------------------------------ CLI helpers

def set_name(normalized: dict, cfg: dict, n: int, name: str) -> dict:
    """`spitball speakers <dir> <n> "Name"` / `--clear`: a user entry
    (kept forever) or, with an empty name, back to automatic. Returns the
    updated map. Raises ValueError for an unknown speaker number. Setting
    a name that a re-transcription had dropped clears that dropped note."""
    order, _ = far_speaker_order(normalized.get("utterances", []), max_speakers(cfg))
    speakers = normalize_map(normalized.get("speakers"), order)
    key = str(n)
    if key not in speakers:
        raise ValueError(f"no far-side speaker {n} (this call has {len(order)})")
    name = (name or "").strip()
    if name:
        speakers[key].update(name=name, confidence="high", source="user", evidence="set by hand")
        dropped = normalized.get("speakers_dropped")
        if isinstance(dropped, list):
            dropped = [d for d in dropped if not (isinstance(d, dict)
                                                 and str(d.get("name") or "").lower() == name.lower())]
            if dropped:
                normalized["speakers_dropped"] = dropped
            else:
                normalized.pop("speakers_dropped", None)
    else:
        speakers[key].update(name="", confidence="none", source="", evidence="")
    normalized["speakers"] = speakers
    return speakers


def learn_name(normalized: dict, cfg: dict, n: int, name: str, before: dict | None = None) -> str:
    """After a hand rename: when far speaker `n` is clearly one invitee,
    remember that invitee's address -> `name` (spitball/people.py's book)
    so later calls use it. Clearly means a 1:1 (one far invitee, one far
    voice), or the speaker's automatic name before the rename was exactly
    one invitee's. Returns the address learned, or "". Never raises."""
    from . import people
    name = (name or "").strip()
    meeting = normalized.get("meeting")
    if not name or "@" in name or not meeting:
        return ""
    try:
        far = [a for a in _far_invitees(meeting) if (a.get("email") or "").strip()]
        order, _ = far_speaker_order(normalized.get("utterances", []), max_speakers(cfg))
        email = ""
        if len(far) == 1 and len(order) == 1 and len(_far_invitees(meeting)) == 1:
            email = far[0]["email"]
        else:
            prior = str((before or {}).get("name") or "").strip().lower()
            if prior and (before or {}).get("source") != "user":
                hits = [c for c in candidates(meeting) if c["email"] and c["name"].lower() == prior]
                if len(hits) == 1:
                    email = hits[0]["email"]
        if email and people.remember(email, name):
            return email.strip().lower()
    except Exception:
        pass
    return ""


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
