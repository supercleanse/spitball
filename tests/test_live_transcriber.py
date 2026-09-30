"""spitball.live tests (docs/SPEC-live-transcript.md). voxtype's own
subprocess calls are mocked throughout (same pattern as
test_provider_local.py); ffmpeg segmentation is left real -- it's already a
hard Spitball dependency, and the whole point of this feature is that
reading a still-growing Ogg file actually works, which is worth proving with
real ffmpeg rather than mocking it away.

"Growing" files are simulated by writing successive byte PREFIXES of a
complete, pre-built stereo Opus file to the path the transcriber watches --
confirmed against a real, `-flush_packets 1`-driven live ffmpeg recording
(see spitball/recorder.py) to decode and grow exactly the same way a
truncated prefix does; this makes the incremental-growth tests deterministic
and fast (no real-time recording needed) while still exercising the real
"decode what's there so far, including a torn trailing page" path.
"""
import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from spitball import audio, config, live
from tests.testutil import isolated_runtime

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
_REAL_RUN = subprocess.run


def _voxtype_stub(text_for):
    """A subprocess.run side_effect standing in for voxtype: `text_for(clip_path)
    -> str` picks the transcript text per clip so tests can trace which
    clip produced which utterance; every non-voxtype command (ffmpeg/ffprobe)
    passes through to the real subprocess.run."""
    def _run(cmd, **kw):
        if cmd and cmd[0] == "voxtype":
            clip = Path(cmd[-1])
            return mock.Mock(returncode=0, stdout=f"Loading audio file: x\n\n{text_for(clip)}\n")
        return _REAL_RUN(cmd, **kw)
    return _run


def _build_two_channel(tmp: Path, ch0_filter: str, ch1_filter: str, duration: float) -> Path:
    """Builds a stereo Opus file from two independent single-channel ffmpeg
    filter graphs (lavfi sources), same amerge approach recorder.py/the other
    tests already use."""
    ch0 = tmp / "ch0.wav"
    ch1 = tmp / "ch1.wav"
    _REAL_RUN(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-filter_complex", ch0_filter, "-map", "[out]", "-t", f"{duration}", str(ch0)],
              check=True, capture_output=True, text=True)
    _REAL_RUN(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-filter_complex", ch1_filter, "-map", "[out]", "-t", f"{duration}", str(ch1)],
              check=True, capture_output=True, text=True)
    out = tmp / "final.opus"
    _REAL_RUN(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-i", str(ch0), "-i", str(ch1),
               "-filter_complex", "[0:a][1:a]amerge=inputs=2[a]",
               "-map", "[a]", "-ac", "2", "-c:a", "libopus", "-b:a", "32k", "-application", "voip",
               str(out)], check=True, capture_output=True, text=True)
    return out


# Tone-silence-tone on channel 0 (mic): speech 0-2s, a real pause 2.0-3.2s
# (1.2s, comfortably over the 0.5s live threshold), speech 3.2-5.2s. Channel 1
# (far side) stays silent throughout -- nothing should ever be transcribed on
# it in these tests.
_CH0_TSTS = ("sine=frequency=440:duration=2[a1];"
             "anullsrc=r=48000:cl=mono,atrim=duration=1.2[s1];"
             "sine=frequency=440:duration=2[a2];"
             "[a1][s1][a2]concat=n=3:v=0:a=1[out]")
_CH1_SILENT = "anullsrc=r=48000:cl=mono[out]"


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe not installed")
class LiveTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp = Path(self.tmpdir.name)
        self.call_dir = self.tmp / "call"
        self.call_dir.mkdir()
        self.audio_path = self.tmp / "audio.opus"
        self._rt_cm = isolated_runtime(self.tmp)
        self._rt_cm.__enter__()
        self.addCleanup(self._rt_cm.__exit__, None, None, None)

    def cfg(self, **overrides):
        c = {"live_transcript": True, "live_max_window_s": 12}
        c.update(overrides)
        return c

    def patched(self):
        return (mock.patch("spitball.providers.local.subprocess.run",
                            side_effect=_voxtype_stub(lambda clip: f"TEXT[{clip.name}]")),
                mock.patch("spitball.providers.local.shutil.which", return_value="/usr/bin/voxtype"),
                mock.patch("spitball.providers.local.info", return_value={"model": "test-model"}))


class TestAvailability(LiveTestCase):
    def test_disabled_setting_publishes_unavailable_without_starting_a_thread(self):
        with mock.patch("spitball.providers.local.shutil.which", return_value="/usr/bin/voxtype"):
            lt = live.LiveTranscriber(self.call_dir, self.audio_path, time.time(),
                                       self.cfg(live_transcript=False))
            lt.start()
        self.assertFalse(lt._running)
        self.assertIsNone(lt._thread)
        state = json.loads(live.live_state_path().read_text())
        self.assertEqual(state["status"], "unavailable")
        self.assertEqual(state["message"], live.DISABLED_MESSAGE)

    def test_voxtype_missing_publishes_unavailable_with_ready_reason(self):
        with mock.patch("spitball.providers.local.shutil.which", return_value=None):
            lt = live.LiveTranscriber(self.call_dir, self.audio_path, time.time(), self.cfg())
            lt.start()
        self.assertFalse(lt._running)
        state = json.loads(live.live_state_path().read_text())
        self.assertEqual(state["status"], "unavailable")
        self.assertEqual(state["message"], live.NOT_AVAILABLE_MESSAGE)

    def test_stop_and_finish_on_a_never_started_transcriber_is_a_safe_noop(self):
        with mock.patch("spitball.providers.local.shutil.which", return_value=None):
            lt = live.LiveTranscriber(self.call_dir, self.audio_path, time.time(), self.cfg())
            lt.start()
            lt.stop_and_finish(timeout=1.0)  # must not raise, must not write .live.json
        self.assertFalse((self.call_dir / live.CALL_LIVE_FILENAME).exists())

    def test_ready_when_available_publishes_listening_and_starts_a_thread(self):
        with mock.patch("spitball.providers.local.shutil.which", return_value="/usr/bin/voxtype"):
            lt = live.LiveTranscriber(self.call_dir, self.audio_path, time.time(), self.cfg())
            lt.start()
            try:
                self.assertTrue(lt._running)
                self.assertIsNotNone(lt._thread)
                self.assertTrue(lt._thread.is_alive())
                state = json.loads(live.live_state_path().read_text())
                self.assertEqual(state["status"], "listening")
            finally:
                lt.stop_and_finish(timeout=5.0)
            self.assertFalse(lt._thread.is_alive())


class TestSegmentationOnAGrowingFile(LiveTestCase):
    """Simulates a growing recording by writing successive byte prefixes of a
    complete file -- see module docstring."""

    def setUp(self):
        super().setUp()
        self.final = _build_two_channel(self.tmp, _CH0_TSTS, _CH1_SILENT, 5.2)
        self.total_size = self.final.stat().st_size
        self.data = self.final.read_bytes()

    def _grow_to(self, fraction: float) -> None:
        n = max(1, int(self.total_size * fraction))
        self.audio_path.write_bytes(self.data[:n])

    def test_each_stretch_transcribed_once_across_incremental_ticks(self):
        patches = self.patched()
        with patches[0], patches[1], patches[2]:
            lt = live.LiveTranscriber(self.call_dir, self.audio_path, time.time(), self.cfg())
            with tempfile.TemporaryDirectory() as scratch:
                tmp = Path(scratch)
                for i, frac in enumerate((0.2, 0.4, 0.6, 0.8, 1.0)):
                    self._grow_to(frac)
                    lt._tick(tmp, final=(frac >= 1.0))

        utts = sorted(lt._utterances, key=lambda u: u["start"])
        # Only channel 0 ever has speech in this fixture.
        self.assertTrue(all(u["channel"] == 0 for u in utts), utts)
        self.assertTrue(all(not u.get("failed") for u in utts), utts)
        # Two speech runs (before/after the 1.2s pause), each transcribed
        # exactly once -- not zero, not twice, and never overlapping.
        self.assertEqual(len(utts), 2, utts)
        first, second = utts
        self.assertAlmostEqual(first["start"], 0.0, delta=0.2)
        self.assertLess(first["end"], 2.5)
        self.assertGreaterEqual(second["start"], first["end"])
        self.assertAlmostEqual(second["end"], 5.2, delta=0.3)
        # Fully resolved through to the end after the final (forced-close) tick.
        self.assertAlmostEqual(lt._resolved[0], lt._last_duration, delta=0.05)
        self.assertAlmostEqual(lt._resolved[1], lt._last_duration, delta=0.05)

    def test_open_segment_is_not_transcribed_until_closed_or_forced(self):
        """Growing only partway into the first speech run (no pause reached
        yet, and short of live_max_window_s) must not produce any utterance
        at all -- it's still open."""
        patches = self.patched()
        with patches[0], patches[1], patches[2]:
            lt = live.LiveTranscriber(self.call_dir, self.audio_path, time.time(), self.cfg())
            with tempfile.TemporaryDirectory() as scratch:
                self._grow_to(0.2)  # ~1s in -- still mid-speech, no pause yet
                lt._tick(Path(scratch), final=False)
        self.assertEqual(lt._utterances, [])

    def test_long_unbroken_speech_is_force_cut_at_max_window(self):
        """No pause at all within max_window_s: the still-open run is cut
        where it stands rather than growing forever."""
        tmp = self.tmp
        ch0 = _build_two_channel(tmp, "sine=frequency=440:duration=6[out]", _CH1_SILENT, 6.0)
        shutil.copy(ch0, self.audio_path)
        patches = self.patched()
        with patches[0], patches[1], patches[2]:
            lt = live.LiveTranscriber(self.call_dir, self.audio_path, time.time(),
                                       self.cfg(live_max_window_s=3))
            with tempfile.TemporaryDirectory() as scratch:
                lt._tick(Path(scratch), final=False)
        utts = sorted(lt._utterances, key=lambda u: u["start"])
        self.assertTrue(utts, "expected a forced cut once the window filled up")
        self.assertLessEqual(utts[0]["end"] - utts[0]["start"], 3.0 + 0.3)
        # Cut where it stands, not waiting for the whole 6s clip.
        self.assertLess(utts[0]["end"], 6.0)

    def test_final_flush_closes_a_still_open_segment_regardless_of_window(self):
        tmp = self.tmp
        ch0 = _build_two_channel(tmp, "sine=frequency=440:duration=2[out]", _CH1_SILENT, 2.0)
        shutil.copy(ch0, self.audio_path)
        patches = self.patched()
        with patches[0], patches[1], patches[2]:
            lt = live.LiveTranscriber(self.call_dir, self.audio_path, time.time(), self.cfg())
            with tempfile.TemporaryDirectory() as scratch:
                tmp2 = Path(scratch)
                lt._tick(tmp2, final=False)
                self.assertEqual(lt._utterances, [])  # still open, no pause, short of 12s
                lt._tick(tmp2, final=True)  # the recording just stopped
        self.assertEqual(len(lt._utterances), 1)
        self.assertAlmostEqual(lt._utterances[0]["end"], 2.0, delta=0.1)


class TestEchoRemoval(LiveTestCase):
    def test_publish_drops_mic_echo_but_call_folder_cache_keeps_raw(self):
        started = time.time()
        with mock.patch("spitball.providers.local.shutil.which", return_value="/usr/bin/voxtype"), \
             mock.patch("spitball.providers.local.info", return_value={"model": "m"}):
            lt = live.LiveTranscriber(self.call_dir, self.audio_path, started, self.cfg())
            lt.start()
            try:
                lt._utterances = [
                    {"channel": 1, "speaker": 0, "start": 1.0, "end": 3.0, "transcript": "let's ship it Friday"},
                    {"channel": 0, "speaker": 0, "start": 1.2, "end": 3.1, "transcript": "lets ship it friday"},
                ]
                lt._resolved = {0: 5.0, 1: 5.0}
                lt._last_duration = 5.0
                published = json.loads(live.live_state_path().read_text())
                # The mic (channel 0) line echoes the far side almost exactly
                # -- _drop_echo should remove it from the live display.
                lt._publish(*lt._compute_status())
                published = json.loads(live.live_state_path().read_text())
                channels = {u["channel"] for u in published["utterances"]}
                self.assertEqual(channels, {1})
            finally:
                lt.stop_and_finish(timeout=5.0)
        # The call-folder cache (what process() will read) keeps BOTH raw --
        # process()'s own build_transcript() does echo removal itself, same
        # as it would for any other provider's output.
        data = json.loads((self.call_dir / live.CALL_LIVE_FILENAME).read_text())
        self.assertEqual(data["provider"], "local")
        self.assertEqual(data["note"], "live transcript")
        self.assertEqual({u["channel"] for u in data["utterances"]}, {0, 1})


class TestCatchingUpStatus(LiveTestCase):
    def test_large_backlog_reports_catching_up(self):
        lt = live.LiveTranscriber(self.call_dir, self.audio_path, time.time(), self.cfg())
        lt._last_duration = 40.0
        lt._resolved = {0: 5.0, 1: 38.0}  # channel 0 is 35s behind
        status, _ = lt._compute_status()
        self.assertEqual(status, "catching-up")

    def test_small_backlog_reports_listening(self):
        lt = live.LiveTranscriber(self.call_dir, self.audio_path, time.time(), self.cfg())
        lt._last_duration = 40.0
        lt._resolved = {0: 39.0, 1: 40.0}
        status, _ = lt._compute_status()
        self.assertEqual(status, "listening")


class TestLiveJsonSchema(LiveTestCase):
    def test_publish_writes_the_documented_shape(self):
        lt = live.LiveTranscriber(self.call_dir, self.audio_path, 1234.0, self.cfg())
        lt._utterances = [{"channel": 0, "speaker": 0, "start": 2.0, "end": 3.0, "transcript": "b"},
                          {"channel": 1, "speaker": 0, "start": 0.0, "end": 1.0, "transcript": "a"}]
        lt._publish("listening", "")
        data = json.loads(live.live_state_path().read_text())
        self.assertEqual(set(data.keys()), {"call_id", "started_at", "status", "message", "utterances"})
        self.assertEqual(data["call_id"], self.call_dir.name)
        self.assertEqual(data["started_at"], 1234)
        # Sorted by start.
        self.assertEqual([u["start"] for u in data["utterances"]], [0.0, 2.0])


class TestStopAndFinish(LiveTestCase):
    def test_stop_flushes_remaining_audio_into_call_folder_live_json(self):
        ch0 = _build_two_channel(self.tmp, "sine=frequency=440:duration=2[out]", _CH1_SILENT, 2.0)
        shutil.copy(ch0, self.audio_path)
        patches = self.patched()
        with patches[0], patches[1], patches[2], \
             mock.patch("spitball.live.POLL_INTERVAL_S", 0.05):
            lt = live.LiveTranscriber(self.call_dir, self.audio_path, time.time(), self.cfg())
            lt.start()
            lt.stop_and_finish(timeout=10.0)
        self.assertFalse(lt._thread.is_alive())
        data = json.loads((self.call_dir / live.CALL_LIVE_FILENAME).read_text())
        self.assertEqual(data["provider"], "local")
        total = sum(u["end"] - u["start"] for u in data["utterances"])
        self.assertGreater(total, 0)
        state = json.loads(live.live_state_path().read_text())
        self.assertEqual(state["status"], "stopped")

    def test_never_more_than_one_thread(self):
        """A single LiveTranscriber only ever owns one background thread --
        calling start() is the only way to create one, and it's only called
        once per instance (Daemon.start() makes a fresh instance per
        recording; see tests/test_daemon.py for the daemon-level guarantee)."""
        with mock.patch("spitball.providers.local.shutil.which", return_value="/usr/bin/voxtype"):
            lt = live.LiveTranscriber(self.call_dir, self.audio_path, time.time(), self.cfg())
            lt.start()
            first_thread = lt._thread
            lt.stop_and_finish(timeout=5.0)
        self.assertIsNotNone(first_thread)
        self.assertFalse(first_thread.is_alive())


if __name__ == "__main__":
    unittest.main()


class _FakeEngine:
    """Stands in for live_engine.Engine: answers every clip with its own
    name so tests can tell engine output from the voxtype stub's."""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail
        self.closed = False

    def transcribe(self, wav):
        self.calls.append(Path(wav).name)
        return None if self.fail else f"FAST[{Path(wav).name}]"

    def alive(self):
        return not self.fail

    def close(self):
        self.closed = True


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe not installed")
class TestFastEnginePartials(LiveTestCase):
    """With a persistent engine, the still-open run is shown as a partial
    that never reaches the stored utterances or the call folder cache."""

    def setUp(self):
        super().setUp()
        self.final = _build_two_channel(self.tmp, _CH0_TSTS, _CH1_SILENT, 5.2)
        self.data = self.final.read_bytes()

    def _grow_to(self, fraction: float) -> None:
        self.audio_path.write_bytes(self.data[:max(1, int(len(self.data) * fraction))])

    def _transcriber(self, engine):
        lt = live.LiveTranscriber(self.call_dir, self.audio_path, time.time(), self.cfg())
        lt._engine = engine
        return lt

    def test_open_run_is_published_as_a_partial_only(self):
        patches = self.patched()
        with patches[0], patches[1], patches[2]:
            lt = self._transcriber(_FakeEngine())
            with tempfile.TemporaryDirectory() as scratch:
                self._grow_to(0.3)  # ~1.5s into the first tone, no pause yet
                lt._tick(Path(scratch), final=False)
        self.assertEqual(lt._utterances, [])
        shown = lt._snapshot_utterances()
        self.assertEqual(len(shown), 1, shown)
        self.assertTrue(shown[0]["partial"])
        self.assertTrue(shown[0]["transcript"].startswith("FAST["))
        lt._write_call_folder_live_json()
        cached = json.loads((self.call_dir / live.CALL_LIVE_FILENAME).read_text())
        self.assertEqual(cached["utterances"], [])

    def test_closing_the_run_finalizes_and_clears_the_partial(self):
        patches = self.patched()
        with patches[0], patches[1], patches[2]:
            lt = self._transcriber(_FakeEngine())
            with tempfile.TemporaryDirectory() as scratch:
                tmp = Path(scratch)
                for frac in (0.3, 0.6, 1.0):
                    self._grow_to(frac)
                    lt._tick(tmp, final=(frac >= 1.0))
        utts = sorted(lt._utterances, key=lambda u: u["start"])
        self.assertEqual(len(utts), 2, utts)
        self.assertTrue(all("partial" not in u for u in utts), utts)
        self.assertTrue(all(u["transcript"].startswith("FAST[") for u in utts), utts)
        self.assertEqual(lt._partials, {0: None, 1: None})
        self.assertFalse(any(u.get("partial") for u in lt._snapshot_utterances()))

    def test_engine_failure_falls_back_to_voxtype(self):
        engine = _FakeEngine(fail=True)
        patches = self.patched()
        with patches[0], patches[1], patches[2]:
            lt = self._transcriber(engine)
            with tempfile.TemporaryDirectory() as scratch:
                self._grow_to(1.0)
                lt._tick(Path(scratch), final=True)
        self.assertIsNone(lt._engine)  # dead engine dropped for the rest of the call
        self.assertTrue(lt._utterances)
        self.assertTrue(all(u["transcript"].startswith("TEXT[") for u in lt._utterances), lt._utterances)

    def test_no_partials_without_an_engine(self):
        patches = self.patched()
        with patches[0], patches[1], patches[2]:
            lt = self._transcriber(None)
            with tempfile.TemporaryDirectory() as scratch:
                self._grow_to(0.3)
                lt._tick(Path(scratch), final=False)
        self.assertEqual(lt._snapshot_utterances(), [])
