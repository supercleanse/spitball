"""On-device speaker split of the far channel (docs/SPEC-v2.md section 4).

The local provider hears the far side as one mixed voice ("Them"). This
module tells those voices apart with sherpa-onnx's offline speaker
diarization -- pyannote's segmentation-3.0 model plus a small speaker
embedding model, both plain files from the k2-fsa GitHub releases (no
Hugging Face account or token) -- and hands the provider one speaker id per
stretch of audio, so a voxtype window that straddles two people is cut in
two before it is transcribed.

Entirely optional, same shape as spitball/live_engine.py: `spitball diarize
setup` installs the `sherpa-onnx` wheel into the live-engine venv
(~/.local/share/spitball/live-engine/venv, created if it isn't there yet)
and downloads the two models, pinned by SHA-256, into
`<engine dir>/models/diarization/`. The rest of Spitball stays standard
library only; the actual inference runs in a short-lived child process
(spitball/diarize_worker.py) under the venv's Python. Anything missing or
failing means `far_channel()` returns no segments and a reason, and the
provider keeps today's behavior -- a bad split is worse than none.

Only the far channel is ever diarized, and never when the calendar says
exactly one other person was invited (a 1:1 has nothing to split). When
the invitee count is known it becomes `num_clusters`, the one knob that
decides most of the quality; otherwise the clustering picks its own count
under a conservative threshold and the result is capped at `speaker_max`.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from . import config, live_engine

WORKER_SCRIPT = config.PROGRAM_ROOT / "spitball" / "diarize_worker.py"
PACKAGES = ("sherpa-onnx>=1.13,<2", "numpy")
ENGINE = "sherpa-onnx"

# Both from k2-fsa's GitHub releases, verified by hash after download.
# Provenance and licenses: models/diarization/README.md.
SEGMENTATION_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
                    "speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2")
SEGMENTATION_TAR_SHA256 = "24615ee884c897d9d2ba09bb4d30da6bb1b15e685065962db5b02e76e4996488"
SEGMENTATION_MEMBER = "sherpa-onnx-pyannote-segmentation-3-0/model.int8.onnx"
SEGMENTATION_SHA256 = "d582f4b4c6b48205de7e0643c57df0df5615a3c176189be3fc461e9d18827b5d"
SEGMENTATION_FILE = "pyannote-segmentation-3.0.int8.onnx"

EMBEDDING_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
                 "speaker-recongition-models/3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx")
EMBEDDING_SHA256 = "c59158379255ad66e161679cca6af8d52d51e389e3224ab7d7a7baae295c2db5"
EMBEDDING_FILE = "3dspeaker-eres2net-en-voxceleb.onnx"

THREADS = 4
# Clustering distance threshold when the speaker count is unknown. Measured
# on k2-fsa's three two-speaker English samples with this embedding model:
# 0.5 over-splits one of them, 0.6-0.9 all find exactly two; 0.7 sits in
# the middle of that band.
DEFAULT_THRESHOLD = 0.7
MIN_DURATION_ON_S = 0.3   # a speech turn shorter than this is dropped
MIN_DURATION_OFF_S = 0.5  # a pause shorter than this doesn't end a turn
# A window is cut at a speaker change only when both resulting pieces are
# at least this long; shorter overlaps are noise in the segmenter's timing
# and the piece goes to whoever spoke most of it.
MIN_PIECE_S = 1.0
USER_AGENT = "spitball-diarize/1.0 (+https://github.com/supercleanse/spitball)"


class DiarizationError(RuntimeError):
    pass


# ------------------------------------------------------------------ install

def model_dir() -> Path:
    return live_engine.ENGINE_DIR / "models" / "diarization"


def segmentation_model() -> Path:
    return model_dir() / SEGMENTATION_FILE


def embedding_model() -> Path:
    return model_dir() / EMBEDDING_FILE


def package_installed() -> bool:
    """Whether the sherpa-onnx wheel is in the live-engine venv -- checked
    on disk, not by importing (that would mean starting the venv's Python
    on every status call)."""
    venv = live_engine.ENGINE_DIR / "venv"
    return live_engine.installed() and any(venv.glob("lib/python*/site-packages/sherpa_onnx"))


def models_present() -> bool:
    return segmentation_model().is_file() and embedding_model().is_file()


def installed() -> bool:
    return package_installed() and models_present()


def status() -> dict:
    """`spitball diarize status --json`."""
    return {"installed": installed(), "package": package_installed(), "models": models_present(),
            "venv": str(live_engine.ENGINE_DIR / "venv"), "model_dir": str(model_dir()),
            "engine": ENGINE}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(url: str, dst: Path, timeout: int = 600) -> None:
    """Streams `url` to `dst` (via a temp file beside it)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dst.name}.", dir=dst.parent)
    tmp = Path(tmp_name)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp, os.fdopen(fd, "wb") as out:
            shutil.copyfileobj(resp, out, 1 << 20)
        os.replace(tmp, dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _fetch_pinned(url: str, sha256: str, dst: Path, download=_download) -> None:
    download(url, dst)
    got = _sha256(dst)
    if got != sha256:
        dst.unlink(missing_ok=True)
        raise DiarizationError(f"{dst.name}: hash mismatch (expected {sha256[:12]}…, got {got[:12]}…)")


def _extract_member(tar_path: Path, member: str, dst: Path) -> None:
    """Pulls one file out of the tarball by reading it directly -- never a
    whole-archive extract, so nothing in the archive can write anywhere
    else."""
    with tarfile.open(tar_path, "r:bz2") as tar:
        try:
            fh = tar.extractfile(member)
        except KeyError:
            fh = None
        if fh is None:
            raise DiarizationError(f"{tar_path.name}: no {member} inside")
        with fh, dst.open("wb") as out:
            shutil.copyfileobj(fh, out, 1 << 20)


def fetch_models(download=_download) -> None:
    """Downloads both models (skipping any already present with the right
    hash). Raises DiarizationError on a hash mismatch or a network error."""
    mdir = model_dir()
    mdir.mkdir(parents=True, exist_ok=True)
    try:
        if not (segmentation_model().is_file() and _sha256(segmentation_model()) == SEGMENTATION_SHA256):
            tar_path = mdir / "segmentation.tar.bz2"
            _fetch_pinned(SEGMENTATION_URL, SEGMENTATION_TAR_SHA256, tar_path, download)
            _extract_member(tar_path, SEGMENTATION_MEMBER, segmentation_model())
            tar_path.unlink(missing_ok=True)
            if _sha256(segmentation_model()) != SEGMENTATION_SHA256:
                segmentation_model().unlink(missing_ok=True)
                raise DiarizationError(f"{SEGMENTATION_FILE}: hash mismatch after extraction")
        if not (embedding_model().is_file() and _sha256(embedding_model()) == EMBEDDING_SHA256):
            _fetch_pinned(EMBEDDING_URL, EMBEDDING_SHA256, embedding_model(), download)
    except urllib.error.URLError as e:
        raise DiarizationError(f"download failed: {getattr(e, 'reason', e)}")
    except (OSError, tarfile.TarError) as e:
        raise DiarizationError(f"download failed: {e}")


def setup(run=subprocess.run, download=_download) -> tuple[bool, str]:
    """`spitball diarize setup`: the sherpa-onnx wheel into the live-engine
    venv (created if missing; the live engine's own packages are left as
    they are), then the two models. Returns (ok, message)."""
    error = live_engine.install(PACKAGES, run)
    if error:
        return False, error
    try:
        fetch_models(download)
    except DiarizationError as e:
        return False, str(e)
    return True, f"Speaker split installed: {ENGINE} in {live_engine.ENGINE_DIR / 'venv'}, models in {model_dir()}"


# ------------------------------------------------------------------ run

def expected_far_speakers(meeting: dict | None) -> int | None:
    """How many people besides the user the invite says were there: every
    attendee that isn't `self` and didn't decline. None when there is no
    meeting or the invite carries no one else."""
    if not meeting or not isinstance(meeting.get("attendees"), list):
        return None
    n = sum(1 for a in meeting["attendees"]
            if isinstance(a, dict) and not a.get("self") and a.get("response") != "declined")
    return n or None


def max_speakers(cfg: dict) -> int:
    try:
        n = int(cfg.get("speaker_max", config.DEFAULTS["speaker_max"]))
    except (TypeError, ValueError):
        n = config.DEFAULTS["speaker_max"]
    return max(1, min(12, n))


def skip_reason(cfg: dict, expected: int | None) -> str:
    """Why the split would not run: "" means go ahead."""
    if cfg.get("speaker_split", True) is False:
        return "off"
    if max_speakers(cfg) <= 1:
        return "speaker_max is 1"
    if expected == 1:
        return "one remote attendee expected"
    if not installed():
        return "not installed"
    return ""


def clusters_for(cfg: dict, expected: int | None) -> int:
    """`num_clusters` for the worker: the invitee count (capped at
    speaker_max) when known, else -1 (pick by threshold)."""
    if expected and expected > 1:
        return min(expected, max_speakers(cfg))
    return -1


def run(wav: Path, num_clusters: int = -1, threshold: float = DEFAULT_THRESHOLD,
        python: Path | None = None, script: Path = WORKER_SCRIPT,
        timeout: float | None = None) -> dict:
    """Diarizes one mono 16 kHz WAV in the venv worker. Returns the worker's
    reply: {"segments": [[start, end, speaker], ...], "seconds": float,
    "speakers": int}. Raises DiarizationError on any failure."""
    python = python or live_engine.venv_python()
    if timeout is None:
        from . import audio as audio_mod
        timeout = max(120.0, 60.0 + 0.6 * audio_mod.ffprobe_duration(wav))
    cmd = [str(python), "-I", str(script), str(segmentation_model()), str(embedding_model()),
           str(wav), str(num_clusters), f"{threshold:.3f}", str(THREADS),
           f"{MIN_DURATION_ON_S}", f"{MIN_DURATION_OFF_S}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise DiarizationError(f"worker timed out after {int(timeout)}s")
    except (OSError, subprocess.SubprocessError) as e:
        raise DiarizationError(f"worker failed to start: {e}")
    reply = None
    for line in (r.stdout or "").splitlines()[::-1]:  # the JSON line is the last one
        line = line.strip()
        if line.startswith("{"):
            try:
                reply = json.loads(line)
            except ValueError:
                reply = None
            break
    if r.returncode != 0 or not isinstance(reply, dict):
        detail = ""
        if isinstance(reply, dict) and reply.get("error"):
            detail = str(reply["error"])
        else:
            lines = (r.stderr or "").strip().splitlines()
            detail = lines[-1] if lines else f"exit {r.returncode}"
        raise DiarizationError(detail)
    if "error" in reply:
        raise DiarizationError(str(reply["error"]))
    segments = []
    for item in reply.get("segments") or []:
        try:
            s, e, spk = float(item[0]), float(item[1]), int(item[2])
        except (TypeError, ValueError, IndexError):
            continue
        if e > s:
            segments.append((s, e, spk))
    segments.sort()
    return {"segments": segments, "seconds": float(reply.get("seconds") or 0.0),
            "speakers": len({spk for _, _, spk in segments})}


def far_channel(wav: Path, cfg: dict, expected: int | None) -> tuple[list, dict]:
    """The provider's entry point: (segments, record). `segments` is
    [(start, end, speaker)] or [] when the split didn't run; `record` is the
    `diarization` block for .transcript.json. Never raises."""
    reason = skip_reason(cfg, expected)
    record = {"ran": False, "engine": ENGINE, "expected": expected}
    if reason:
        record["reason"] = reason
        return [], record
    num_clusters = clusters_for(cfg, expected)
    record["num_clusters"] = num_clusters
    try:
        result = run(wav, num_clusters=num_clusters)
    except DiarizationError as e:
        record["reason"] = f"failed: {e}"
        return [], record
    segments = cap_speakers(result["segments"], max_speakers(cfg))
    record.update({"ran": True, "found": len({spk for _, _, spk in segments}),
                   "seconds": round(result["seconds"], 2)})
    if not segments:
        record["reason"] = "no speech found on the far channel"
    return segments, record


# ------------------------------------------------------------------ interval math

def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def dominant_speaker(start: float, end: float, segments: list) -> int | None:
    """The speaker with the most seconds inside [start, end), or None when
    no segment touches it."""
    totals: dict = {}
    for s, e, spk in segments:
        if e <= start:
            continue
        if s >= end:
            break
        o = _overlap(start, end, s, e)
        if o > 0:
            totals[spk] = totals.get(spk, 0.0) + o
    if not totals:
        return None
    return max(totals, key=lambda k: (totals[k], -k))


def cap_speakers(segments: list, limit: int) -> list:
    """Keeps the `limit` speakers with the most talk time; each other
    speaker's segments go to the kept speaker nearest in time."""
    if limit < 1 or not segments:
        return segments
    talk: dict = {}
    for s, e, spk in segments:
        talk[spk] = talk.get(spk, 0.0) + (e - s)
    if len(talk) <= limit:
        return segments
    keep = set(sorted(talk, key=lambda k: (-talk[k], k))[:limit])
    kept = [(s, e, spk) for s, e, spk in segments if spk in keep]
    out = []
    for s, e, spk in segments:
        if spk in keep:
            out.append((s, e, spk))
            continue
        mid = (s + e) / 2
        nearest = min(kept, key=lambda k: min(abs(k[0] - mid), abs(k[1] - mid)))
        out.append((s, e, nearest[2]))
    out.sort()
    return out


def _timeline(start: float, end: float, segments: list) -> list:
    """The speaker in charge across [start, end) as [(t0, t1, speaker)],
    from the segments' boundaries (overlaps resolved to whoever's segment
    started first)."""
    points = {start, end}
    inside = []
    for s, e, spk in segments:
        if e <= start or s >= end:
            continue
        inside.append((max(s, start), min(e, end), spk))
        points.add(max(s, start))
        points.add(min(e, end))
    if not inside:
        return []
    cuts = sorted(points)
    out = []
    for t0, t1 in zip(cuts, cuts[1:]):
        if t1 - t0 <= 0:
            continue
        who = None
        for s, e, spk in inside:
            if s <= t0 and e >= t1:
                who = spk
                break
        if who is None:
            continue
        if out and out[-1][2] == who and abs(out[-1][1] - t0) < 1e-6:
            out[-1] = (out[-1][0], t1, who)
        else:
            out.append((t0, t1, who))
    return out


def plan_windows(windows: list, segments: list, min_piece_s: float = MIN_PIECE_S) -> list:
    """Maps the provider's speech windows onto the diarized segments:
    [(start, end, speaker)] where a window that straddles a speaker change
    is cut at the change (when both pieces are at least `min_piece_s`), and
    every piece carries whoever spoke most of it. A window no segment
    touches keeps the speaker of the piece before it (or 0)."""
    out = []
    last = 0
    for start, end in windows:
        pieces = [(start, end)]
        for t0, t1, _ in _timeline(start, end, segments)[1:]:
            ps, pe = pieces[-1]
            if t0 - ps >= min_piece_s and pe - t0 >= min_piece_s:
                pieces[-1] = (ps, t0)
                pieces.append((t0, pe))
        for ps, pe in pieces:
            who = dominant_speaker(ps, pe, segments)
            if who is None:
                who = last
            out.append((ps, pe, who))
            last = who
    return out


def split_transcript(audio: Path, normalized: dict, cfg: dict, expected: int | None,
                     transcribe_piece=None) -> dict:
    """The pipeline's entry point for a transcript that already exists (the
    live transcriber's `.live.json`, reused instead of a full
    transcription): splits the far channel off `audio`, diarizes it, labels
    every far utterance with its dominant speaker, and, when
    `transcribe_piece(wav, start, end, tmp, tag, speaker)` is given,
    re-transcribes the utterances that straddle a speaker change as
    separate pieces. Returns `normalized` (mutated) with a `diarization`
    block; any failure leaves the utterances as they were."""
    reason = skip_reason(cfg, expected)
    if reason:
        normalized["diarization"] = {"ran": False, "engine": ENGINE, "expected": expected, "reason": reason}
        return normalized
    from . import audio as audio_mod
    try:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            _, right = audio_mod.split_stereo_to_mono_wavs(audio, tmp)
            segments, record = far_channel(right, cfg, expected)
            if segments:
                utts, straddlers = label_utterances(normalized.get("utterances", []), segments)
                resplit = 0
                if transcribe_piece is not None:
                    for i, pieces in straddlers:
                        new = []
                        for k, (s, e, spk) in enumerate(pieces):
                            new += transcribe_piece(right, s, e, tmp, f"resplit-{i}-{k}", spk)
                        if new and not all(u.get("failed") for u in new):
                            utts[i] = new
                            resplit += 1
                flat = []
                for u in utts:
                    flat.extend(u if isinstance(u, list) else [u])
                normalized["utterances"] = sorted(flat, key=lambda u: (u["start"], u.get("channel", 0)))
                record["resplit"] = resplit
            normalized["diarization"] = record
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        normalized["diarization"] = {"ran": False, "engine": ENGINE, "expected": expected,
                                     "reason": f"failed: {e}"}
    return normalized


def label_utterances(utterances: list, segments: list, min_piece_s: float = MIN_PIECE_S) -> tuple:
    """For a transcript that already exists (the reused live transcript):
    gives every far-side utterance the speaker who spoke most of it and
    reports which ones straddle a change big enough to be worth splitting
    (a caller with the audio can re-transcribe those pieces). Returns
    (utterances, straddlers) where straddlers is [(index, [(start, end,
    speaker), ...])]."""
    out = []
    straddlers = []
    last = 0
    for i, u in enumerate(utterances):
        u = dict(u)
        if u.get("channel") == 1 and not u.get("failed"):
            pieces = plan_windows([(u["start"], u["end"])], segments, min_piece_s)
            who = dominant_speaker(u["start"], u["end"], segments)
            u["speaker"] = last if who is None else who
            last = u["speaker"]
            if len(pieces) > 1:
                straddlers.append((i, pieces))
        out.append(u)
    return out, straddlers
