"""Live transcription while a call is recording (docs/SPEC-live-transcript.md).

A background thread, started by Daemon.start() alongside the recorder and
stopped by Daemon.stop(), watches the growing `audio.opus`, transcribes each
channel's speech in near real time with the *local* (voxtype) provider --
regardless of `transcription_provider`, so live transcription never costs
money or sends audio anywhere -- and publishes
`$XDG_RUNTIME_DIR/spitball/live.json` for the bar widget's Live popup. Never
more than one of these runs at once, and it never runs voxtype concurrently
with itself or with process()'s own transcription: Daemon.stop() joins this
thread (bounded by a time limit) before deciding whether process() can reuse
its output.

Segmentation is "closed by a pause": every ~2s (POLL_INTERVAL_S) each
channel's newly-decoded tail (from wherever the previous tick left off, to
however much of `audio.opus` ffprobe currently reports as decodable -- Ogg is
readable mid-write, verified empirically; a torn trailing page just means
`ffprobe`/`ffmpeg` see slightly less than the true live edge, not a hard
failure) is split into speech runs by ffmpeg silencedetect (0.5s minimum
silence, per spec). A run followed by a detected pause is closed and
transcribed immediately via `providers.local._transcribe_window()` -- the
exact same retry/halving/failure-marker/log-stripping logic the local
provider's normal post-call pass uses, just handed a small tail clip instead
of a whole-call one. The one run still touching the current decode boundary
(nothing has closed it yet) stays open until either a pause closes it or it
grows past `live_max_window_s`, at which point it's cut where it stands. This
keeps each stretch of audio transcribed exactly once -- nothing is ever
re-examined once a tick has resolved it, closed or not (see `_resolved`).

Fast path (spitball/live_engine.py): when the live-engine venv is set up and
voxtype is on a Parakeet model, the thread first loads that model once in a
persistent worker and transcribes through it instead of spawning `voxtype
transcribe` per segment (~0.1-0.5s a segment instead of ~2s). With it, ticks
run every FAST_POLL_INTERVAL_S and the still-open run is also transcribed as
it grows and published as a `"partial": true` utterance, so the popup shows
the current sentence forming rather than waiting for the pause. Partials
are display-only: they never enter `_utterances` or the call folder's
`.live.json`, and the closed segment is transcribed fresh when it closes.
Any worker failure drops back to the voxtype path for the rest of the call.
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
from pathlib import Path

from . import audio as audio_mod
from . import config
from . import live_engine
from .process import _drop_echo
from .providers import local as local_provider

LIVE_STATE_FILENAME = "live.json"
CALL_LIVE_FILENAME = ".live.json"

NOT_AVAILABLE_MESSAGE = "Live transcript needs voxtype"
DISABLED_MESSAGE = "Live transcript is turned off"

POLL_INTERVAL_S = 2.0
FAST_POLL_INTERVAL_S = 0.5       # with the persistent engine (see live_engine.py)
MIN_PARTIAL_S = 0.6              # an open run shorter than this isn't worth a partial yet
PARTIAL_STEP_S = 0.4             # re-transcribe an open run once it's grown this much
MIN_SILENCE_S = 0.5              # per spec: a pause closes a segment at 0.5s
MIN_SEGMENT_S = 0.4              # matches audio.raw_speech_segments' own default
CATCH_UP_THRESHOLD_S = 20.0      # per spec: "more than ~20s of closed audio waiting"
DEFAULT_MAX_WINDOW_S = 12.0
MIN_NEW_AUDIO_S = 0.05           # below this there's nothing worth a tick yet
STOP_FINISH_TIMEOUT_S = 10.0     # bounds Daemon.stop()'s wait for the final flush

CHANNELS = (0, 1)


def live_state_path() -> Path:
    """`$XDG_RUNTIME_DIR/spitball/live.json`. Always resolved off
    config.RUNTIME_DIR at call time (never cached at import/construction) so
    test isolation via tests/testutil.isolated_runtime() -- which reassigns
    that module attribute -- redirects this the same way it already
    redirects providers.local's model_state_path()."""
    return config.RUNTIME_DIR / LIVE_STATE_FILENAME


def _shift(utterances: list, offset: float) -> list:
    """Copies of `utterances` with start/end shifted by `offset` -- turns a
    tail clip's own 0-based timestamps into absolute call time."""
    out = []
    for u in utterances:
        v = dict(u)
        v["start"] = v.get("start", 0.0) + offset
        v["end"] = v.get("end", 0.0) + offset
        out.append(v)
    return out


class LiveTranscriber:
    """One instance per recording. `start()` decides immediately whether live
    transcription can actually run (the `live_transcript` setting, and
    whether voxtype is on PATH) and publishes live.json accordingly either
    way, so the popup always has something sensible to show. `stop_and_finish()`
    is the only way to stop it."""

    def __init__(self, call_dir: Path, audio_path: Path, started_at: float, cfg: dict):
        self.call_dir = call_dir
        self.audio_path = audio_path
        self.started_at = started_at
        self.call_id = call_dir.name
        self.cfg = cfg
        self.max_window_s = float(cfg.get("live_max_window_s") or DEFAULT_MAX_WINDOW_S)
        self.enabled = bool(cfg.get("live_transcript", True))
        # local_provider.ready() is used only as the availability CHECK (""
        # means voxtype is on PATH) -- its own message text is written for
        # the general setup_needed slot ("Install dictation (voxtype)..."),
        # not this popup's status line, so substitute NOT_AVAILABLE_MESSAGE
        # (the exact wording docs/SPEC-live-transcript.md gives as an
        # example) when it isn't.
        self._unavailable_reason = "" if self.enabled else DISABLED_MESSAGE
        if self.enabled and not self._unavailable_reason and local_provider.ready(cfg):
            self._unavailable_reason = NOT_AVAILABLE_MESSAGE

        self._lock = threading.RLock()
        self._utterances: list[dict] = []       # absolute-time, unfiltered (echo removal applied at publish)
        self._resolved = {c: 0.0 for c in CHANNELS}  # per channel: everything before this is fully resolved
        self._last_duration = 0.0
        self._engine: live_engine.Engine | None = None
        self._partials: dict = {c: None for c in CHANNELS}   # per channel: the open run's latest partial
        self._partial_len = {c: 0.0 for c in CHANNELS}       # how long that run was when last transcribed
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._unavailable_reason:
            self._publish("unavailable", self._unavailable_reason)
            return
        self._running = True
        self._publish("listening", "")
        self._thread = threading.Thread(target=self._run, name="live-transcript", daemon=True)
        self._thread.start()

    def stop_and_finish(self, timeout: float = STOP_FINISH_TIMEOUT_S) -> None:
        """Signals the loop to stop and waits (bounded by `timeout`) for its
        final flush over whatever's left. Called from Daemon.stop() AFTER the
        recorder itself has stopped (so `audio_path` is complete, not still
        being written) and BEFORE process() runs, so process() either sees a
        finished, usable `.live.json` or cleanly finds none/an unusable one
        and falls back -- and so this thread's last voxtype call never
        overlaps process()'s own. A no-op if the transcriber never actually
        started (disabled, or voxtype unavailable) -- there is nothing to
        flush and no thread to join."""
        self._stop_event.set()
        if not self._running:
            return
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._write_call_folder_live_json()

    # ------------------------------------------------------------ the loop

    def _run(self) -> None:
        try:
            self._engine = live_engine.open_engine(self.cfg)
        except Exception:  # noqa: BLE001 -- the engine is optional; voxtype is the fallback
            self._engine = None
        try:
            with tempfile.TemporaryDirectory(prefix="spitball-live-") as tmp_str:
                tmp = Path(tmp_str)
                while not self._stop_event.is_set():
                    began = time.monotonic()
                    self._tick(tmp, final=False)
                    self._publish(*self._compute_status())
                    # A steady beat: the interval runs from the start of
                    # this tick, not its end, so a slow tick doesn't also
                    # add a full interval of waiting on top.
                    interval = FAST_POLL_INTERVAL_S if self._engine else POLL_INTERVAL_S
                    self._stop_event.wait(max(0.05, interval - (time.monotonic() - began)))
                # The recording is over: one last pass, forcing any still-open
                # segment closed since nothing more is ever coming.
                self._tick(tmp, final=True)
        finally:
            if self._engine is not None:
                self._engine.close()
                self._engine = None
        self._publish("stopped", "")

    def _transcribe(self, tail: Path, channel: int, start: float, end: float,
                    tmp: Path, tag: str) -> list:
        """[start, end) of the channel's tail clip as utterances -- through the
        persistent engine when there is one, else the voxtype path."""
        engine = self._engine
        if engine is not None:
            clip = tmp / f"{tag}.wav"
            try:
                audio_mod.extract_clip(tail, start, end, clip)
                text = engine.transcribe(clip)
            except Exception:  # noqa: BLE001
                text = None
            finally:
                clip.unlink(missing_ok=True)
            if text is not None:
                return [{"channel": channel, "speaker": 0, "start": start, "end": end,
                         "transcript": text}] if text else []
            if not engine.alive():
                self._engine = None  # it died -- voxtype for the rest of the call
        return local_provider._transcribe_window(tail, channel, start, end, tmp, tag)

    def _partial_covering(self, channel: int, start: float, end: float) -> dict | None:
        """The channel's current partial as a finished utterance, if it was
        transcribed from this same run and already reaches its end -- then
        closing the run needs no second model call. The partial's clip may
        run a little past `end` (into the pause), which only adds silence."""
        with self._lock:
            p = self._partials[channel]
        if p is None or abs(p["start"] - start) >= 0.05 or p["end"] < end - 0.05:
            return None
        done = {k: v for k, v in p.items() if k != "partial"}
        done["end"] = end
        return done

    def _update_partial(self, tail: Path, channel: int, resolved: float, start: float,
                        end: float, tmp: Path) -> None:
        """Transcribes the still-open run [start, end) of the tail clip for
        display, if it's long enough and has grown since the last partial."""
        run_len = end - start
        with self._lock:
            current = self._partials[channel]
            same_run = current is not None and abs(current["start"] - (resolved + start)) < 0.05
            grown = run_len - (self._partial_len[channel] if same_run else 0.0)
        if run_len < MIN_PARTIAL_S or grown < PARTIAL_STEP_S:
            return
        utts = self._transcribe(tail, channel, start, end, tmp,
                                f"live-ch{channel}-partial-{int(round((resolved + start) * 1000))}")
        with self._lock:
            if utts and utts[0].get("transcript") and not utts[0].get("failed"):
                partial = _shift(utts, resolved)[0]
                partial["partial"] = True
                self._partials[channel] = partial
            self._partial_len[channel] = run_len

    def _tick(self, tmp: Path, final: bool) -> None:
        duration = audio_mod.ffprobe_duration(self.audio_path)
        if duration <= 0:
            return  # not decodable yet (e.g. a torn trailing page) -- retried next tick
        with self._lock:
            self._last_duration = duration
        for channel in CHANNELS:
            self._tick_channel(channel, duration, tmp, final)

    def _tick_channel(self, channel: int, duration: float, tmp: Path, final: bool) -> None:
        with self._lock:
            resolved = self._resolved[channel]
        if duration - resolved < MIN_NEW_AUDIO_S:
            return

        tail = tmp / f"live-tail-ch{channel}.wav"
        try:
            audio_mod.extract_channel_clip(self.audio_path, channel, resolved, duration, tail)
            tail_duration = audio_mod.ffprobe_duration(tail)
        except Exception:
            return  # transient ffmpeg hiccup on the growing file -- retry next tick
        if tail_duration <= 0:
            return

        silences = audio_mod.detect_silence(tail, min_silence_s=MIN_SILENCE_S)
        segments = audio_mod.raw_speech_segments(tail_duration, silences, min_segment_s=MIN_SEGMENT_S)

        new_utts: list[dict] = []
        new_resolved = duration  # default: nothing pending -- fully resolved through this tick's boundary
        left_open = False
        for i, (s, e) in enumerate(segments):
            is_last = i == len(segments) - 1
            open_tail = is_last and (tail_duration - e) < MIN_NEW_AUDIO_S
            if open_tail:
                run_len = tail_duration - s
                if not final and run_len < self.max_window_s:
                    new_resolved = resolved + s  # still growing -- leave it, re-examine next tick
                    left_open = True
                    if self._engine is not None:
                        self._update_partial(tail, channel, resolved, s, tail_duration, tmp)
                    break
                # Either the recording just stopped (final=True, so
                # everything left gets closed regardless), or the run has
                # already reached live_max_window_s -- cut it there rather
                # than wherever this tick's decode boundary happens to land,
                # so a later tick that caught up a lot of audio at once
                # doesn't swallow more than one window in a single cut.
                end_local = tail_duration if final else min(tail_duration, s + self.max_window_s)
            else:
                end_local = e
            tag = f"live-ch{channel}-{int(round((resolved + s) * 1000))}"
            reused = self._partial_covering(channel, resolved + s, resolved + end_local)
            if reused is not None:
                new_utts.append(reused)
            else:
                utts = self._transcribe(tail, channel, s, end_local, tmp, tag)
                new_utts.extend(_shift(utts, resolved))
            if open_tail:
                new_resolved = resolved + end_local  # a forced cut short of tail_duration stays open past here
                break

        tail.unlink(missing_ok=True)
        with self._lock:
            self._utterances.extend(new_utts)
            self._resolved[channel] = new_resolved
            if not left_open:
                self._partials[channel] = None
                self._partial_len[channel] = 0.0

    # ------------------------------------------------------------ status/publish

    def _compute_status(self) -> tuple[str, str]:
        with self._lock:
            backlog = max(0.0, self._last_duration - min(self._resolved.values()))
        if backlog > CATCH_UP_THRESHOLD_S:
            return "catching-up", ""
        return "listening", ""

    def _snapshot_utterances(self) -> list[dict]:
        """Sorted, echo-filtered (spec: "apply the same echo removal as
        build_transcript incrementally") -- this is the DISPLAY copy for
        live.json. The call-folder `.live.json` cache keeps the raw,
        unfiltered utterances instead (see _write_call_folder_live_json), so
        process()'s own build_transcript() applies echo removal itself, the
        same as it would for any other provider's output."""
        with self._lock:
            partials = [p for p in self._partials.values() if p]
            utts = sorted(self._utterances + partials, key=lambda u: u["start"])
        return _drop_echo(utts)

    def _publish(self, status: str, message: str) -> None:
        payload = {
            "call_id": self.call_id,
            "started_at": int(self.started_at),
            "status": status,
            "message": message,
            "utterances": self._snapshot_utterances(),
        }
        config.atomic_write(live_state_path(), json.dumps(payload))

    def _write_call_folder_live_json(self) -> None:
        with self._lock:
            utts = sorted(self._utterances, key=lambda u: u["start"])
        data = {
            "provider": "local",
            "model": local_provider.info().get("model") or "",
            "utterances": utts,
            "note": "live transcript",
        }
        config.atomic_write(self.call_dir / CALL_LIVE_FILENAME, json.dumps(data))
