"""spitball.audio tests: pure math (speech_windows, chunk_ranges) with no
subprocess at all, plus real ffmpeg/ffprobe exercises for the rest (ffmpeg is
already a hard dependency, same pattern as test_process.py's
TestAudioSeconds and test_recorder.py). Never touches tests/.cache/ or any
network -- generates its own tiny throwaway WAVs.
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spitball import audio


class TestChunkRanges(unittest.TestCase):
    def test_under_max_is_one_chunk(self):
        self.assertEqual(audio.chunk_ranges(500.0, max_chunk_s=1200.0), [(0.0, 500.0)])

    def test_exact_multiple_splits_evenly(self):
        self.assertEqual(audio.chunk_ranges(2400.0, max_chunk_s=1200.0),
                          [(0.0, 1200.0), (1200.0, 2400.0)])

    def test_remainder_chunk_is_shorter(self):
        ranges = audio.chunk_ranges(2500.0, max_chunk_s=1200.0)
        self.assertEqual(ranges, [(0.0, 1200.0), (1200.0, 2400.0), (2400.0, 2500.0)])

    def test_zero_or_negative_duration_is_empty(self):
        self.assertEqual(audio.chunk_ranges(0.0), [])
        self.assertEqual(audio.chunk_ranges(-5.0), [])


class TestSpeechWindows(unittest.TestCase):
    def test_no_silence_is_one_window(self):
        self.assertEqual(audio.speech_windows(10.0, []), [(0.0, 10.0)])

    def test_long_unbroken_speech_is_cut_to_max_window(self):
        # Regression: a mic channel with no pause (steady echo/background) came
        # out as one 497 s window, and Parakeet failed on it.
        windows = audio.speech_windows(518.0, [(20.2, 20.8)])
        self.assertTrue(all(e - s <= 30.0 + 1e-6 for s, e in windows))
        self.assertAlmostEqual(windows[0][0], 0.0)
        self.assertAlmostEqual(windows[-1][1], 518.0)
        for (_, e1), (s2, _) in zip(windows, windows[1:]):
            self.assertLessEqual(e1, s2 + 1e-6)

    def test_split_evenly(self):
        self.assertEqual(audio.split_evenly((0.0, 30.0), 30.0), [(0.0, 30.0)])
        self.assertEqual(audio.split_evenly((10.0, 70.0), 30.0), [(10.0, 40.0), (40.0, 70.0)])
        self.assertEqual(len(audio.split_evenly((0.0, 61.0), 30.0)), 3)

    def test_silence_splits_into_segments(self):
        # Speech 0-5, silence 5-8, speech 8-12.
        windows = audio.speech_windows(12.0, [(5.0, 8.0)])
        self.assertEqual(windows, [(0.0, 12.0)])  # bridged: both segments fit under max_window_s

    def test_short_segments_dropped(self):
        # A 0.2s blip between two long silences never becomes its own window.
        windows = audio.speech_windows(20.0, [(0.0, 5.0), (5.2, 20.0)], min_segment_s=0.4)
        self.assertEqual(windows, [])

    def test_windows_capped_at_max_window_s(self):
        # Speech runs 0-50 with tiny silences every 10s (each just over the
        # 0.6s ffmpeg minimum) -- windows should stop growing past ~30s.
        silences = [(9.9, 10.6), (19.9, 20.6), (29.9, 30.6), (39.9, 40.6)]
        windows = audio.speech_windows(50.0, silences, max_window_s=30.0)
        for start, end in windows:
            self.assertLessEqual(end - start, 30.0 + 1e-6)
        # The whole 50s span is still covered, start to end.
        self.assertEqual(windows[0][0], 0.0)
        self.assertEqual(windows[-1][1], 50.0)

    def test_zero_duration_is_empty(self):
        self.assertEqual(audio.speech_windows(0.0, []), [])

    def test_silence_at_very_start_and_end_trimmed(self):
        windows = audio.speech_windows(20.0, [(0.0, 3.0), (17.0, 20.0)])
        self.assertEqual(windows, [(3.0, 17.0)])


class TestDetectSilenceParsing(unittest.TestCase):
    """Parses ffmpeg's own stderr log format without needing a real
    subprocess -- the shape it emits is stable and worth locking down."""

    def test_parses_start_end_pairs(self):
        fake_stderr = (
            "[silencedetect @ 0x1] silence_start: 1.5\n"
            "[silencedetect @ 0x1] silence_end: 3.25 | silence_duration: 1.75\n"
            "[silencedetect @ 0x1] silence_start: 10\n"
            "[silencedetect @ 0x1] silence_end: 11.1 | silence_duration: 1.1\n"
        )
        result = mock.Mock(stderr=fake_stderr)
        with mock.patch("spitball.audio.subprocess.run", return_value=result):
            spans = audio.detect_silence(Path("/fake.wav"))
        self.assertEqual(spans, [(1.5, 3.25), (10.0, 11.1)])

    def test_no_silence_lines_is_empty(self):
        result = mock.Mock(stderr="some other ffmpeg noise\n")
        with mock.patch("spitball.audio.subprocess.run", return_value=result):
            self.assertEqual(audio.detect_silence(Path("/fake.wav")), [])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe not installed")
class TestRealFfmpegHelpers(unittest.TestCase):
    """Exercises the real subprocess calls with tiny generated tones --
    ffmpeg/ffprobe are already a hard Spitball dependency (README), so this
    is the same category of test as test_process.py's TestAudioSeconds."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _make_stereo_tone(self, seconds=2.0):
        path = self.dir / "stereo.wav"
        os.system(f"ffmpeg -hide_banner -loglevel error -f lavfi "
                  f"-i \"sine=frequency=440:duration={seconds}\" "
                  f"-f lavfi -i \"sine=frequency=880:duration={seconds}\" "
                  f"-filter_complex \"[0:a][1:a]amerge=inputs=2[a]\" -map \"[a]\" -ac 2 "
                  f"{path}")
        return path

    def test_ffprobe_duration_reads_real_file(self):
        path = self.dir / "mono.wav"
        os.system(f"ffmpeg -hide_banner -loglevel error -f lavfi -i sine=duration=1.5 {path}")
        self.assertAlmostEqual(audio.ffprobe_duration(path), 1.5, delta=0.2)

    def test_ffprobe_duration_missing_file_is_zero(self):
        self.assertEqual(audio.ffprobe_duration(Path("/no/such/file.wav")), 0.0)

    def test_split_stereo_to_mono_wavs_produces_two_mono_files(self):
        stereo = self._make_stereo_tone()
        left, right = audio.split_stereo_to_mono_wavs(stereo, self.dir)
        self.assertTrue(left.exists())
        self.assertTrue(right.exists())
        self.assertNotEqual(left, right)

    def test_extract_clip_cuts_expected_length(self):
        path = self.dir / "mono.wav"
        os.system(f"ffmpeg -hide_banner -loglevel error -f lavfi -i sine=duration=5 {path}")
        clip = self.dir / "clip.wav"
        audio.extract_clip(path, 1.0, 3.0, clip)
        self.assertTrue(clip.exists())
        self.assertAlmostEqual(audio.ffprobe_duration(clip), 2.0, delta=0.2)

    def test_encode_opus_produces_ogg(self):
        path = self.dir / "mono.wav"
        os.system(f"ffmpeg -hide_banner -loglevel error -f lavfi -i sine=duration=1 {path}")
        ogg = self.dir / "out.ogg"
        audio.encode_opus(path, ogg, bitrate="24k")
        self.assertTrue(ogg.exists())
        self.assertGreater(ogg.stat().st_size, 0)

    def test_detect_silence_finds_real_gap(self):
        # 1s tone, 1.5s silence (over the 0.6s min), 1s tone.
        path = self.dir / "with_gap.wav"
        os.system(
            "ffmpeg -hide_banner -loglevel error "
            "-f lavfi -i \"sine=frequency=440:duration=1\" "
            "-f lavfi -i \"anullsrc=r=44100:cl=mono\" "
            "-f lavfi -i \"sine=frequency=440:duration=1\" "
            "-filter_complex \"[1:a]atrim=duration=1.5[sil];[0:a][sil][2:a]concat=n=3:v=0:a=1[a]\" "
            f"-map \"[a]\" {path}")
        spans = audio.detect_silence(path, min_silence_s=0.6)
        self.assertTrue(spans, "expected at least one detected silence span")
        start, end = spans[0]
        self.assertAlmostEqual(start, 1.0, delta=0.3)
        self.assertAlmostEqual(end, 2.5, delta=0.3)


if __name__ == "__main__":
    unittest.main()
