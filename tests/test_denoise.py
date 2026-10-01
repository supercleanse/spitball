"""spitball.denoise tests (docs/SPEC-v2.md section 3): the settings, the
filter chain and its afftdn fallback, the noise-floor gate, the live gate's
hysteresis, prepare_mic's never-raise contract -- all with mocked subprocess
-- plus real ffmpeg exercises on tiny generated WAVs (ffmpeg is a hard
dependency, same category as test_audio.py's TestRealFfmpegHelpers). No
committed audio fixtures: everything is synthesized at test time.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spitball import audio, denoise

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


class TestSettings(unittest.TestCase):
    def test_mode_default_and_normalization(self):
        self.assertEqual(denoise.mode({}), "auto")
        self.assertEqual(denoise.mode(None), "auto")
        self.assertEqual(denoise.mode({"mic_denoise": "off"}), "off")
        self.assertEqual(denoise.mode({"mic_denoise": " ON "}), "on")
        self.assertEqual(denoise.mode({"mic_denoise": "loud"}), "auto")
        self.assertEqual(denoise.mode({"mic_denoise": None}), "auto")

    def test_threshold_default_and_clamp(self):
        self.assertEqual(denoise.threshold_db({}), -45.0)
        self.assertEqual(denoise.threshold_db({"mic_noise_floor_db": -50}), -50.0)
        self.assertEqual(denoise.threshold_db({"mic_noise_floor_db": "-38"}), -38.0)
        self.assertEqual(denoise.threshold_db({"mic_noise_floor_db": -200}), -80.0)
        self.assertEqual(denoise.threshold_db({"mic_noise_floor_db": 5}), -20.0)
        self.assertEqual(denoise.threshold_db({"mic_noise_floor_db": "abc"}), -45.0)


class TestFilterChain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_vendored_model_is_the_default(self):
        self.assertTrue(denoise.MODEL_PATH.is_file(), denoise.MODEL_PATH)
        chain, name = denoise.filter_chain()
        self.assertEqual(name, "arnndn")
        self.assertTrue(chain.startswith("arnndn=m="), chain)
        self.assertTrue(chain.endswith(f":mix={denoise.MIX}"), chain)
        self.assertEqual(denoise.MIX, 0.7)

    def test_missing_model_falls_back_to_afftdn(self):
        chain, name = denoise.filter_chain(self.dir / "nope.rnnn")
        self.assertEqual(name, "afftdn")
        self.assertEqual(chain, denoise.AFFTDN_CHAIN)
        self.assertTrue(chain.startswith("afftdn="))

    def test_never_anlmdn(self):
        # anlmdn aborts in ffmpeg 9.0.1 ("double free or corruption"); it
        # must never appear in any chain Spitball can build.
        for chain in (denoise.filter_chain()[0], denoise.AFFTDN_CHAIN, audio.MIC_FILTER):
            self.assertNotIn("anlmdn", chain)

    def test_escape_filter_path_double_escapes_specials(self):
        # Verified against ffmpeg 9: the graph parser and then the option
        # tokenizer each strip one level, so every special needs two.
        self.assertEqual(denoise.escape_filter_path("a'b:c"), "a\\\\\\'b\\\\\\:c")
        self.assertEqual(denoise.escape_filter_path("/plain/path.rnnn"), "/plain/path.rnnn")
        self.assertEqual(denoise.escape_filter_path("x,y;z[w]=v"),
                         "x\\\\\\,y\\\\\\;z\\\\\\[w\\\\\\]\\\\\\=v")

    def test_chain_uses_escaped_model_path(self):
        model = self.dir / "odd:name.rnnn"
        model.write_text("rnnoise-nu model file version 1\n")
        chain, name = denoise.filter_chain(model)
        self.assertEqual(name, "arnndn")
        self.assertIn(denoise.escape_filter_path(model), chain)
        self.assertNotIn(str(model), chain)  # the raw ':' would break the graph


class TestNoiseFloorMath(unittest.TestCase):
    def test_floor_needs_enough_frames(self):
        self.assertIsNone(denoise.noise_floor_db([]))
        self.assertIsNone(denoise.noise_floor_db([-40.0] * (denoise.MIN_FLOOR_FRAMES - 1)))
        self.assertEqual(denoise.noise_floor_db([-40.0] * denoise.MIN_FLOOR_FRAMES), -40.0)

    def test_floor_is_the_tenth_percentile(self):
        levels = [float(-i) for i in range(100)]  # 0 .. -99
        self.assertEqual(denoise.noise_floor_db(levels), -89.0)   # sorted index 10 of 100
        self.assertEqual(denoise.speech_level_db(levels), -9.0)   # sorted index 90

    def test_digital_silence_reads_as_minus_100(self):
        levels = [audio.DIGITAL_SILENCE_DB] * 30 + [-20.0] * 30
        self.assertEqual(denoise.noise_floor_db(levels), -100.0)

    def test_should_apply(self):
        self.assertTrue(denoise.should_apply("on", None, -45))
        self.assertTrue(denoise.should_apply("on", -80, -45))
        self.assertFalse(denoise.should_apply("off", -10, -45))
        self.assertFalse(denoise.should_apply("auto", None, -45))
        self.assertFalse(denoise.should_apply("auto", -60, -45))
        self.assertFalse(denoise.should_apply("auto", -45, -45))
        self.assertTrue(denoise.should_apply("auto", -44, -45))
        self.assertTrue(denoise.should_apply("auto", -30, -45))

    def test_adaptive_silence_gate(self):
        self.assertEqual(denoise.adaptive_silence_db(None), -35)
        self.assertEqual(denoise.adaptive_silence_db(-60), -35)   # never below the default
        self.assertEqual(denoise.adaptive_silence_db(-45), -35)
        self.assertEqual(denoise.adaptive_silence_db(-40), -30)   # 10 dB above the floor
        self.assertEqual(denoise.adaptive_silence_db(-25), -20)   # capped
        self.assertEqual(denoise.adaptive_silence_db(-5), -20)

    def test_has_speech(self):
        frame = audio.LEVEL_FRAME_S
        levels = [-50.0] * 40
        levels[10] = -20.0  # one loud frame at 0.5-0.55 s
        self.assertTrue(denoise.has_speech(levels, 0.0, 1.0, -50.0))
        self.assertFalse(denoise.has_speech(levels, 0.0, 0.4, -50.0))
        self.assertFalse(denoise.has_speech(levels, 0.6, 2.0, -50.0))
        # Within the 6 dB margin of the floor is still "just the room".
        levels[10] = -45.0
        self.assertFalse(denoise.has_speech(levels, 0.0, 1.0, -50.0))
        levels[10] = -43.9
        self.assertTrue(denoise.has_speech(levels, 0.0, 1.0, -50.0))
        # Unknown floor or no measurements: never drop a window.
        self.assertTrue(denoise.has_speech(levels, 0.0, 1.0, None))
        self.assertTrue(denoise.has_speech([], 0.0, 1.0, -50.0))
        # A window past the end of the measurements is kept, not dropped.
        self.assertTrue(denoise.has_speech(levels, 40 * frame + 5, 40 * frame + 6, -50.0))

    def test_record_shape(self):
        rec = denoise.record_for("auto", True, "arnndn", -38.26, -45.0, speech=-21.04)
        self.assertEqual(rec, {"mode": "auto", "applied": True, "filter": "arnndn",
                               "noise_floor_db": -38.3, "threshold_db": -45.0,
                               "speech_level_db": -21.0})
        rec = denoise.record_for("off", False, "arnndn", None, -45.0)
        self.assertEqual(rec["filter"], None)  # never names a filter that didn't run
        self.assertIsNone(rec["noise_floor_db"])
        self.assertNotIn("error", rec)
        rec = denoise.record_for("on", False, None, -30.0, -45.0, error="boom")
        self.assertEqual(rec["error"], "boom")


class TestPrepareMic(unittest.TestCase):
    """The decision layer, with the ffmpeg passes mocked out."""

    def setUp(self):
        self.wav = Path("/fake/channel-0.wav")
        self.dst = Path("/fake/channel-0-denoised.wav")

    def test_off_measures_nothing_and_applies_nothing(self):
        with mock.patch("spitball.denoise.audio_mod.measure_levels") as measure, \
             mock.patch("spitball.denoise.apply") as apply:
            path, rec = denoise.prepare_mic(self.wav, {"mic_denoise": "off"}, self.dst)
        measure.assert_not_called()
        apply.assert_not_called()
        self.assertEqual(path, self.wav)
        self.assertEqual(rec["mode"], "off")
        self.assertFalse(rec["applied"])

    def test_auto_quiet_mic_is_left_alone(self):
        with mock.patch("spitball.denoise.audio_mod.measure_levels", return_value=[-60.0] * 100), \
             mock.patch("spitball.denoise.apply") as apply:
            path, rec = denoise.prepare_mic(self.wav, {}, self.dst)
        apply.assert_not_called()
        self.assertEqual(path, self.wav)
        self.assertEqual(rec["mode"], "auto")
        self.assertFalse(rec["applied"])
        self.assertEqual(rec["noise_floor_db"], -60.0)
        self.assertEqual(rec["threshold_db"], -45.0)

    def test_auto_noisy_mic_gets_the_filter(self):
        with mock.patch("spitball.denoise.audio_mod.measure_levels", return_value=[-30.0] * 100), \
             mock.patch("spitball.denoise.apply", return_value="arnndn") as apply:
            path, rec = denoise.prepare_mic(self.wav, {"mic_denoise": "auto"}, self.dst)
        apply.assert_called_once_with(self.wav, self.dst)
        self.assertEqual(path, self.dst)
        self.assertTrue(rec["applied"])
        self.assertEqual(rec["filter"], "arnndn")
        self.assertEqual(rec["noise_floor_db"], -30.0)

    def test_auto_respects_a_custom_threshold(self):
        with mock.patch("spitball.denoise.audio_mod.measure_levels", return_value=[-50.0] * 100), \
             mock.patch("spitball.denoise.apply", return_value="arnndn") as apply:
            _, rec = denoise.prepare_mic(self.wav, {"mic_noise_floor_db": -55}, self.dst)
        apply.assert_called_once()
        self.assertTrue(rec["applied"])
        self.assertEqual(rec["threshold_db"], -55.0)

    def test_on_applies_even_when_quiet(self):
        with mock.patch("spitball.denoise.audio_mod.measure_levels", return_value=[-90.0] * 100), \
             mock.patch("spitball.denoise.apply", return_value="afftdn") as apply:
            path, rec = denoise.prepare_mic(self.wav, {"mic_denoise": "on"}, self.dst)
        apply.assert_called_once()
        self.assertEqual(path, self.dst)
        self.assertEqual(rec, {"mode": "on", "applied": True, "filter": "afftdn",
                               "noise_floor_db": -90.0, "threshold_db": -45.0,
                               "speech_level_db": -90.0})

    def test_filter_failure_keeps_the_raw_copy_and_never_raises(self):
        with mock.patch("spitball.denoise.audio_mod.measure_levels", return_value=[-30.0] * 100), \
             mock.patch("spitball.denoise.apply", side_effect=RuntimeError("arnndn failed (x); afftdn failed (y)")):
            path, rec = denoise.prepare_mic(self.wav, {"mic_denoise": "on"}, self.dst)
        self.assertEqual(path, self.wav)
        self.assertFalse(rec["applied"])
        self.assertIsNone(rec["filter"])
        self.assertIn("afftdn failed", rec["error"])

    def test_precomputed_levels_skip_the_measurement(self):
        with mock.patch("spitball.denoise.audio_mod.measure_levels") as measure, \
             mock.patch("spitball.denoise.apply", return_value="arnndn"):
            _, rec = denoise.prepare_mic(self.wav, {}, self.dst, levels=[-20.0] * 100)
        measure.assert_not_called()
        self.assertTrue(rec["applied"])


class TestApplyFallback(unittest.TestCase):
    """RNNoise first, afftdn when RNNoise can't run, an error only when both
    fail -- with ffmpeg mocked so the failure paths are reachable."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.model = self.dir / "m.rnnn"
        self.model.write_text("rnnoise-nu model file version 1\n")
        self.src = self.dir / "in.wav"
        self.dst = self.dir / "out.wav"

    def _fail(self, cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd, stderr="ffmpeg: no such filter")

    def test_arnndn_first_then_afftdn(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            if len(calls) == 1:
                self._fail(cmd)
            return mock.Mock(returncode=0)

        with mock.patch("spitball.denoise.subprocess.run", side_effect=run), \
             mock.patch("spitball.denoise.audio_mod.ffprobe_duration", return_value=12.5):
            name = denoise.apply(self.src, self.dst, self.model)
        self.assertEqual(name, "afftdn")
        self.assertEqual(len(calls), 2)
        first_af = calls[0][calls[0].index("-af") + 1]
        second_af = calls[1][calls[1].index("-af") + 1]
        self.assertIn("arnndn=m=", first_af)
        self.assertTrue(first_af.startswith(f"apad=pad_dur={denoise.TAIL_PAD_S},"), first_af)
        self.assertIn("afftdn", second_af)
        self.assertNotIn("arnndn", second_af)
        for cmd in calls:
            self.assertIn("-t", cmd)
            self.assertEqual(cmd[cmd.index("-t") + 1], "12.500000")
            self.assertEqual(cmd[cmd.index("-ar") + 1], "16000")
            self.assertEqual(cmd[-1], str(self.dst))

    def test_both_failing_raises_with_both_reasons(self):
        with mock.patch("spitball.denoise.subprocess.run", side_effect=self._fail), \
             mock.patch("spitball.denoise.audio_mod.ffprobe_duration", return_value=3.0):
            with self.assertRaises(RuntimeError) as cm:
                denoise.apply(self.src, self.dst, self.model)
        self.assertIn("arnndn failed", str(cm.exception))
        self.assertIn("afftdn failed", str(cm.exception))
        self.assertIn("no such filter", str(cm.exception))

    def test_missing_model_goes_straight_to_afftdn(self):
        calls = []
        with mock.patch("spitball.denoise.subprocess.run", side_effect=lambda cmd, **kw: calls.append(cmd)), \
             mock.patch("spitball.denoise.audio_mod.ffprobe_duration", return_value=3.0):
            name = denoise.apply(self.src, self.dst, self.dir / "missing.rnnn")
        self.assertEqual(name, "afftdn")
        self.assertEqual(len(calls), 1)
        self.assertIn("afftdn", calls[0][calls[0].index("-af") + 1])

    def test_afftdn_failure_without_a_model_raises(self):
        with mock.patch("spitball.denoise.subprocess.run", side_effect=self._fail), \
             mock.patch("spitball.denoise.audio_mod.ffprobe_duration", return_value=3.0):
            with self.assertRaises(RuntimeError) as cm:
                denoise.apply(self.src, self.dst, self.dir / "missing.rnnn")
        self.assertTrue(str(cm.exception).startswith("afftdn failed"))

    def test_unknown_duration_skips_the_trim(self):
        calls = []
        with mock.patch("spitball.denoise.subprocess.run", side_effect=lambda cmd, **kw: calls.append(cmd)), \
             mock.patch("spitball.denoise.audio_mod.ffprobe_duration", return_value=0.0):
            denoise.apply(self.src, self.dst, self.model)
        self.assertNotIn("-t", calls[0])


class TestLiveGate(unittest.TestCase):
    def test_off_never_measures_or_applies(self):
        g = denoise.LiveGate({"mic_denoise": "off"})
        self.assertFalse(g.measures)
        self.assertFalse(g.feed([-10.0] * 500))
        self.assertFalse(g.active)
        rec = g.record()
        self.assertEqual((rec["mode"], rec["applied"], rec["filter"], rec["noise_floor_db"]),
                         ("off", False, None, None))

    def test_on_is_always_active_without_measuring(self):
        g = denoise.LiveGate({"mic_denoise": "on"})
        self.assertFalse(g.measures)
        self.assertTrue(g.feed(None))
        self.assertTrue(g.feed([]))
        g.applied("arnndn")
        rec = g.record()
        self.assertEqual((rec["mode"], rec["applied"], rec["filter"]), ("on", True, "arnndn"))
        self.assertIsNone(rec["noise_floor_db"])

    def test_auto_waits_for_enough_audio_then_follows_the_floor(self):
        g = denoise.LiveGate({})
        self.assertTrue(g.measures)
        self.assertFalse(g.feed([-30.0] * 10))       # 0.5 s: not enough to judge
        self.assertFalse(g.feed([-60.0] * 100))      # quiet room
        self.assertFalse(g.active)
        self.assertFalse(g.feed([-30.0] * 200))      # the quiet frames still hold the 10th percentile
        self.assertTrue(g.feed([-30.0] * 1200))      # the rolling minute is now all noise
        self.assertTrue(g.active)
        rec = g.record()
        self.assertFalse(rec["applied"])              # nothing applied yet -- the caller reports that
        self.assertEqual(rec["noise_floor_db"], -30.0)
        self.assertEqual(rec["speech_level_db"], -30.0)

    def test_auto_hysteresis(self):
        g = denoise.LiveGate({"mic_noise_floor_db": -45})
        self.assertTrue(g.feed([-30.0] * 1200))
        self.assertTrue(g.feed([-46.0] * 1200))      # just under the threshold: stays on
        self.assertFalse(g.feed([-49.0] * 1200))     # more than 3 dB under: off
        self.assertFalse(g.feed([-45.5] * 1200))     # under the threshold: stays off
        self.assertTrue(g.feed([-44.0] * 1200))

    def test_rolling_window_is_capped(self):
        g = denoise.LiveGate({})
        g.feed([-30.0] * 5000)
        self.assertEqual(len(g._levels), int(denoise.LIVE_WINDOW_S / audio.LEVEL_FRAME_S))

    def test_failure_is_recorded(self):
        g = denoise.LiveGate({"mic_denoise": "on"})
        g.failed("arnndn failed (x); afftdn failed (y)")
        rec = g.record()
        self.assertFalse(rec["applied"])
        self.assertIn("afftdn failed", rec["error"])


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe not installed")
class TestRealFfmpeg(unittest.TestCase):
    """Generated audio through the real filters: the gate's measurements
    land where they should, RNNoise runs at 16 kHz in and out, the padding
    trick leaves no NaN click at the tail, and the fallback really works."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _gen(self, name: str, graph: str, seconds: float) -> Path:
        path = self.dir / name
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-filter_complex", graph, "-map", "[out]", "-t", f"{seconds}",
                        "-ar", "16000", "-ac", "1", str(path)], check=True, capture_output=True)
        return path

    def _nans(self, path: Path) -> int:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-i", str(path), "-af",
                            "aformat=sample_fmts=flt,astats=measure_perchannel=none:measure_overall=Number_of_NaNs",
                            "-f", "null", "-"], capture_output=True, text=True)
        for line in r.stderr.splitlines():
            if "Number of NaNs" in line:
                return int(float(line.split(":")[-1]))
        self.fail("astats printed no NaN count")

    # ffmpeg's `sine` source is 1/8 full scale (about -21 dBFS RMS); +6 dB
    # puts the "voice" near -15 dBFS RMS, a normal speaking level.
    # Tone bursts with digital-silence gaps: the quiet-headset case.
    _QUIET = ("sine=frequency=440:duration=1:sample_rate=16000,volume=6dB[a1];"
              "anullsrc=r=16000:cl=mono,atrim=duration=1[s1];"
              "sine=frequency=440:duration=1:sample_rate=16000,volume=6dB[a2];"
              "anullsrc=r=16000:cl=mono,atrim=duration=1[s2];"
              "[a1][s1][a2][s2]concat=n=4:v=0:a=1[out]")
    # The same bursts over steady pink noise near -36 dBFS: the fan/cafe case.
    _NOISY = ("sine=frequency=440:duration=1:sample_rate=16000,volume=6dB[a1];"
              "anullsrc=r=16000:cl=mono,atrim=duration=1[s1];"
              "sine=frequency=440:duration=1:sample_rate=16000,volume=6dB[a2];"
              "anullsrc=r=16000:cl=mono,atrim=duration=1[s2];"
              "[a1][s1][a2][s2]concat=n=4:v=0:a=1[tone];"
              "anoisesrc=color=pink:sample_rate=16000:amplitude=0.1:seed=3,atrim=duration=4[n];"
              "[tone][n]amix=inputs=2:duration=first:normalize=0[out]")
    # A continuous tone over the same noise, long enough to cut any length from.
    _NOISY_LONG = ("sine=frequency=440:duration=8:sample_rate=16000,volume=6dB[tone];"
                   "anoisesrc=color=pink:sample_rate=16000:amplitude=0.1:seed=3,atrim=duration=8[n];"
                   "[tone][n]amix=inputs=2:duration=first:normalize=0[out]")

    def test_measure_levels_frames_and_digital_silence(self):
        tone = self._gen("tone.wav", "sine=frequency=440:duration=2:sample_rate=16000,volume=6dB[out]", 2.0)
        levels = audio.measure_levels(tone)
        self.assertEqual(len(levels), 40)                 # 2 s / 50 ms
        # A steady sine: every frame reads the same, at a plausible level
        # (ffmpeg's sine source is 1/8 full scale, so +6 dB lands near -15).
        self.assertTrue(all(-18.0 < v < -12.0 for v in levels), levels[:5])
        self.assertLess(max(levels) - min(levels), 0.5)
        silence = self._gen("silence.wav", "anullsrc=r=16000:cl=mono[out]", 1.0)
        self.assertEqual(audio.measure_levels(silence), [audio.DIGITAL_SILENCE_DB] * 20)
        self.assertEqual(audio.measure_levels(self.dir / "missing.wav"), [])

    def test_quiet_gaps_measure_low_and_stay_untouched(self):
        quiet = self._gen("quiet.wav", self._QUIET, 4.0)
        levels = audio.measure_levels(quiet)
        floor = denoise.noise_floor_db(levels)
        self.assertLessEqual(floor, -60.0)
        self.assertGreater(denoise.speech_level_db(levels), -18.0)
        path, rec = denoise.prepare_mic(quiet, {}, self.dir / "dn.wav")
        self.assertEqual(path, quiet)
        self.assertFalse(rec["applied"])
        self.assertFalse((self.dir / "dn.wav").exists())

    def test_noisy_floor_trips_the_gate_and_rnnoise_runs(self):
        noisy = self._gen("noisy.wav", self._NOISY, 4.0)
        levels = audio.measure_levels(noisy)
        floor = denoise.noise_floor_db(levels)
        self.assertGreater(floor, -45.0)
        self.assertLess(floor, -25.0)
        dst = self.dir / "dn.wav"
        path, rec = denoise.prepare_mic(noisy, {}, dst, levels=levels)
        self.assertEqual(path, dst)
        self.assertTrue(rec["applied"])
        self.assertEqual(rec["filter"], "arnndn")
        self.assertAlmostEqual(audio.ffprobe_duration(dst), 4.0, delta=0.02)
        self.assertEqual(self._nans(dst), 0)
        # Still 16 kHz mono, and the tone is still there for the transcriber.
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=sample_rate,channels",
                              "-of", "default=nw=1", str(dst)], capture_output=True, text=True).stdout
        self.assertIn("sample_rate=16000", out)
        self.assertIn("channels=1", out)
        # (mix=0.7 keeps at least 30% of the original, so the tone can lose
        # at most ~10.5 dB even if RNNoise took it for noise.)
        self.assertGreater(denoise.speech_level_db(audio.measure_levels(dst)),
                           denoise.speech_level_db(levels) - 12.0)
        # The source copy is untouched.
        self.assertEqual(audio.measure_levels(noisy), levels)

    def test_odd_length_clips_have_no_tail_click(self):
        # Regression: without the apad/-t guard, arnndn's flush of a final
        # partial frame left ~176 NaN samples (a full-scale click) at the end.
        for seconds in (2.017, 5.123, 0.73):
            src = self._gen(f"odd-{seconds}.wav", self._NOISY_LONG, seconds)
            dst = self.dir / f"odd-{seconds}-dn.wav"
            self.assertEqual(denoise.apply(src, dst), "arnndn")
            self.assertEqual(self._nans(dst), 0, seconds)
            self.assertAlmostEqual(audio.ffprobe_duration(dst), seconds, delta=0.01)

    def test_afftdn_fallback_really_runs(self):
        noisy = self._gen("noisy.wav", self._NOISY, 2.0)
        dst = self.dir / "fft.wav"
        self.assertEqual(denoise.apply(noisy, dst, self.dir / "missing.rnnn"), "afftdn")
        self.assertAlmostEqual(audio.ffprobe_duration(dst), 2.0, delta=0.02)
        self.assertEqual(self._nans(dst), 0)

    def test_model_path_with_shell_specials_still_uses_rnnoise(self):
        odd_dir = self.dir / "weird dir:with,chars;and'quote[x]"
        odd_dir.mkdir()
        model = odd_dir / "sh.rnnn"
        shutil.copy(denoise.MODEL_PATH, model)
        noisy = self._gen("noisy.wav", self._NOISY, 1.0)
        self.assertEqual(denoise.apply(noisy, self.dir / "odd.wav", model), "arnndn")


if __name__ == "__main__":
    unittest.main()
