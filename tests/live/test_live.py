"""Live tests: exercise real PipeWire, real ffmpeg, real voxtype/Deepgram, and
the real local summary model configured in the live user's own
~/.config/spitball/config.json. Only runs with `tests/run.sh --live` or
SPITBALL_LIVE=1 -- skipped entirely otherwise, including in every offline
run and in CI.

Never touches the live daemon: it reads the live config file read-only (for
credentials only, via config.secret()) and never opens the live runtime
socket, state file, or ~/Calls. Every recording/process here writes into its
own tempfile.TemporaryDirectory().

Phase 1 ships exactly two transcription providers (local/voxtype and
deepgram, per spec §8) -- there's no OpenAI/AssemblyAI/Soniox live test here
because there's no such provider in this build yet (see docs/ROADMAP.md).
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from spitball import config, live, process
from spitball.providers import deepgram
from spitball.providers import local as local_provider
from spitball.recorder import Recording
from spitball import detect
from tests.live.live_fixtures import two_channel_sample
from tests.testutil import isolated_runtime

LIVE = os.environ.get("SPITBALL_LIVE") == "1"
SKIP_REASON = "SPITBALL_LIVE not set -- pass --live to tests/run.sh to include live tests"

# tests/__init__.py redirects config.CONFIG_FILE's default to a session-temp
# path (a safety net so a stray background thread from another test can never
# reach the real file) -- so live tests, which explicitly need the real
# user's credentials, read ~/.config/spitball/config.json directly instead of
# through config.load()/config.CONFIG_FILE.
REAL_CONFIG_FILE = Path.home() / ".config" / "spitball" / "config.json"


def load_real_config() -> dict:
    cfg = dict(config.DEFAULTS)
    try:
        cfg.update(json.loads(REAL_CONFIG_FILE.read_text()))
    except (OSError, ValueError):
        pass
    return cfg


def _has(*tools):
    return all(shutil.which(t) for t in tools)


@unittest.skipUnless(LIVE, SKIP_REASON)
class TestRealCallDetection(unittest.TestCase):
    def test_fake_zoom_stream_is_detected(self):
        if not _has("parecord", "pactl"):
            self.skipTest("parecord/pactl not available")
        env = dict(os.environ)
        env["PULSE_PROP"] = "application.name=zoom application.process.binary=zoom"
        proc = subprocess.Popen(["parecord", "--channels=1", "/dev/null"], env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            found = set()
            deadline = time.time() + 5
            while time.time() < deadline:
                found = detect.call_apps(config.DEFAULT_CALL_APPS)
                if "Zoom" in found:
                    break
                time.sleep(0.2)
            self.assertIn("Zoom", found)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


@unittest.skipUnless(LIVE, SKIP_REASON)
class TestRealRecording(unittest.TestCase):
    def test_three_second_recording_is_two_channel_opus(self):
        if not _has("ffmpeg", "ffprobe", "pactl"):
            self.skipTest("ffmpeg/ffprobe/pactl not available")
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "audio.opus"
            rec = Recording(audio, bitrate="32k")
            try:
                time.sleep(3)
                self.assertTrue(rec.alive(), "recorder ffmpeg exited early")
            finally:
                rec.stop()
            self.assertTrue(audio.exists())
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "stream=channels,sample_rate,codec_name",
                 "-of", "default=nw=1", str(audio)],
                capture_output=True, text=True, timeout=15).stdout
            self.assertIn("codec_name=opus", out)
            self.assertIn("channels=2", out)
            # Opus containers always signal the RFC 6716 canonical 48 kHz rate
            # regardless of the input sample rate ffmpeg was asked to encode
            # at (recorder.py requests 16000 on each pulse input) -- confirmed
            # empirically on this machine, so we assert the real invariant
            # (a valid, present sample rate) rather than a specific number.
            self.assertIn("sample_rate=", out)

    def test_own_stream_is_never_detected(self):
        if not _has("ffmpeg", "ffprobe", "pactl"):
            self.skipTest("ffmpeg/ffprobe/pactl not available")
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "audio.opus"
            rec = Recording(audio, bitrate="32k")
            try:
                time.sleep(1.5)
                found = detect.call_apps({"spitball": "SpitballItself"})
                self.assertEqual(found, set())
            finally:
                rec.stop()


@unittest.skipUnless(LIVE, SKIP_REASON)
class TestRealDeepgram(unittest.TestCase):
    def setUp(self):
        if not _has("ffmpeg", "ffprobe"):
            self.skipTest("ffmpeg/ffprobe not available")
        self.cfg = load_real_config()  # the live user's real config -- read-only
        self.key = config.secret(self.cfg, "deepgram_api_key", env="DEEPGRAM_API_KEY")
        if not self.key:
            self.skipTest("no Deepgram key resolvable from the live config")

    def test_two_speakers_both_labeled(self):
        audio = two_channel_sample()
        cfg = dict(self.cfg)
        cfg["deepgram_api_key"] = self.key
        raw = deepgram.raw_transcribe(audio, cfg)
        dg_cache = audio.with_name("two_channel_sample.deepgram.json")
        dg_cache.write_text(json.dumps(raw))  # reused by the full-process test below

        utterances = raw.get("results", {}).get("utterances", [])
        self.assertTrue(utterances, "Deepgram returned no utterances at all")
        channels = {u["channel"] for u in utterances}
        self.assertEqual(channels, {0, 1}, "expected speech detected on both channels")

        normalized = deepgram.normalize(raw, cfg)
        self.assertEqual(normalized["provider"], "deepgram")
        lines = process.build_transcript(normalized, cfg)
        speakers = {w for _, w, _ in lines}
        self.assertIn(cfg["my_name"], speakers)
        self.assertIn("Them", speakers)


@unittest.skipUnless(LIVE, SKIP_REASON)
class TestRealLocalProvider(unittest.TestCase):
    """The local (voxtype) provider on the same cached two-channel sample --
    whatever engine/model the live machine's voxtype is actually configured
    with (Spitball never picks one itself, per spec §7)."""

    def setUp(self):
        if not _has("ffmpeg", "ffprobe", "voxtype"):
            self.skipTest("ffmpeg/ffprobe/voxtype not available")

    def test_both_channels_transcribed_and_labeled_by_time(self):
        audio = two_channel_sample()
        cfg = load_real_config()
        cfg["transcription_provider"] = "local"

        normalized = local_provider.transcribe(audio, cfg)
        self.assertEqual(normalized["provider"], "local")
        channels = {u["channel"] for u in normalized["utterances"]}
        self.assertEqual(channels, {0, 1}, "expected speech detected on both channels")
        for u in normalized["utterances"]:
            self.assertTrue(u["transcript"].strip(), f"empty transcript on channel {u['channel']}")

        lines = process.build_transcript(normalized, cfg)
        self.assertTrue(lines)
        # channel 0 (my mic, spacewalk.wav) starts at 0s; channel 1 (the far
        # side, bueller.wav) is delayed a few seconds -- so ordered by time,
        # my_name's line comes first and "Them" appears somewhere after it.
        speakers_in_order = [w for _, w, _ in lines]
        self.assertEqual(speakers_in_order[0], cfg["my_name"])
        self.assertIn("Them", speakers_in_order)


@unittest.skipUnless(LIVE, SKIP_REASON)
class TestRealPickFolderPortal(unittest.TestCase):
    """Confirms the xdg-desktop-portal FileChooser interface is actually
    introspectable on this machine -- never opens a real dialog (that would
    block on a human, which a live test suite must not do)."""

    def test_portal_filechooser_interface_is_introspectable(self):
        if not shutil.which("gdbus"):
            self.skipTest("gdbus not available")
        out = subprocess.run(
            ["gdbus", "introspect", "--session", "--dest", "org.freedesktop.portal.Desktop",
             "--object-path", "/org/freedesktop/portal/desktop"],
            capture_output=True, text=True, timeout=15).stdout
        self.assertIn("FileChooser", out)


@unittest.skipUnless(LIVE, SKIP_REASON)
class TestRealSummary(unittest.TestCase):
    def setUp(self):
        self.cfg = load_real_config()
        base = self.cfg.get("summary_base_url", "")
        if not base:
            self.skipTest("no summary_base_url configured")

    def test_real_model_returns_markdown_summary(self):
        transcript = ("**[00:00:00] Them:** Hey, thanks for hopping on.\n\n"
                       "**[00:00:05] Me:** Of course, glad to help with the roadmap.\n\n"
                       "**[00:00:12] Them:** Let's ship the export feature Friday.\n\n"
                       "**[00:00:18] Me:** Sounds good, I'll own that.")
        out = process.summarize(transcript, "**Date:** a test call", self.cfg)
        self.assertTrue(out.strip().startswith("#"), out[:200])


@unittest.skipUnless(LIVE, SKIP_REASON)
class TestFullProcessEndToEnd(unittest.TestCase):
    def test_process_pipeline_with_export_off(self):
        if not _has("ffmpeg", "ffprobe"):
            self.skipTest("ffmpeg/ffprobe not available")
        cfg = load_real_config()
        key = config.secret(cfg, "deepgram_api_key", env="DEEPGRAM_API_KEY")
        if not key:
            self.skipTest("no Deepgram key resolvable from the live config")
        if not cfg.get("summary_base_url"):
            self.skipTest("no summary_base_url configured")

        audio = two_channel_sample()
        dg_cache = audio.with_name("two_channel_sample.deepgram.json")

        with tempfile.TemporaryDirectory() as tmp:
            call_dir = Path(tmp) / "2026-09-28-1000-manual"
            call_dir.mkdir()
            shutil.copy(audio, call_dir / "audio.opus")
            if dg_cache.exists():  # reuse the cached transcript so we don't re-pay Deepgram
                shutil.copy(dg_cache, call_dir / ".deepgram.json")
            meta = {"app": "", "started_at": time.time() - 20, "duration": 20.0}
            cfg = dict(cfg)
            cfg["transcription_provider"] = "deepgram"
            cfg["deepgram_api_key"] = key
            cfg["export_dir"] = ""  # explicitly off

            result = process.process(call_dir, meta, cfg)

            final_dir = Path(result["dir"])
            self.assertTrue((final_dir / "audio.opus").exists())
            self.assertTrue((final_dir / "transcript.md").exists())
            summary_text = Path(result["summary"]).read_text()
            self.assertTrue(summary_text.strip().startswith("#"), summary_text[:200])


@unittest.skipUnless(LIVE, SKIP_REASON)
class TestRealLiveTranscriber(unittest.TestCase):
    """docs/SPEC-live-transcript.md's gated live coverage: a real recording
    with the live transcriber thread actually attached to it. Never touches
    the running daemon -- this builds its own Recording and LiveTranscriber
    directly, inside isolated_runtime() so live.json lands under a throwaway
    temp dir, never the real $XDG_RUNTIME_DIR/spitball. Plays nothing (no
    fake-Zoom audio content needed here -- this is about the mechanics of
    reading a growing file and finishing cleanly, not transcription
    accuracy, which TestRealLocalProvider above already covers on a fixed
    file)."""

    def setUp(self):
        if not _has("ffmpeg", "ffprobe", "pactl", "voxtype"):
            self.skipTest("ffmpeg/ffprobe/pactl/voxtype not available")
        # Extra safety net on top of the operator's own pre-flight check
        # (see docs on running this suite): refuse to run at all if the real
        # daemon says a call is actually recording right now. A query is
        # read-only and never touches the daemon's own recording state.
        try:
            r = subprocess.run(["spitball", "status", "--json"], capture_output=True,
                                text=True, timeout=5)
            if r.returncode == 0 and json.loads(r.stdout).get("state") == "recording":
                self.skipTest("a real call is recording right now -- refusing to run")
        except (OSError, ValueError, subprocess.SubprocessError):
            pass  # spitball not on PATH / daemon not reachable -- nothing to protect against

    def test_20s_recording_runs_the_live_thread_and_leaves_valid_audio(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            call_dir = tmp / "2026-09-28-1000-manual"
            call_dir.mkdir()
            audio = call_dir / "audio.opus"
            with isolated_runtime(tmp):
                rec = Recording(audio, bitrate="32k")
                self.assertTrue(rec.alive(), "recorder ffmpeg exited early")
                cfg = {"live_transcript": True, "live_max_window_s": 12}
                lt = live.LiveTranscriber(call_dir, audio, rec.started_at, cfg)
                lt.start()
                self.assertTrue(lt._running, "live transcriber didn't start (voxtype ready?)")
                try:
                    time.sleep(20)
                    self.assertTrue(rec.alive(), "recorder died during the test")
                    # Mid-recording: live.json should exist and be well-formed
                    # (status is either keeping up or catching up -- both fine).
                    mid = json.loads(live.live_state_path().read_text())
                    self.assertEqual(mid["call_id"], call_dir.name)
                    self.assertIn(mid["status"], ("listening", "catching-up"))
                    self.assertIsInstance(mid["utterances"], list)
                finally:
                    rec.stop()
                    lt.stop_and_finish(timeout=20.0)

                final_state = json.loads(live.live_state_path().read_text())
                self.assertEqual(final_state["status"], "stopped")

                live_json = json.loads((call_dir / ".live.json").read_text())
                self.assertEqual(live_json["provider"], "local")
                self.assertIsInstance(live_json["utterances"], list)

            # The recording's own file must still be a valid, playable stereo
            # Opus file after all that incremental reading of it mid-write.
            self.assertTrue(audio.exists())
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "stream=channels,codec_name",
                 "-of", "default=nw=1", str(audio)],
                capture_output=True, text=True, timeout=15).stdout
            self.assertIn("codec_name=opus", out)
            self.assertIn("channels=2", out)


@unittest.skipUnless(LIVE, SKIP_REASON)
class TestRealLiveTranscriberOnGrowingSample(unittest.TestCase):
    """Feeds the cached two-channel sample (real speech on both channels,
    channel 1 delayed a few seconds -- see live_fixtures.two_channel_sample)
    through the live pipeline as a simulated growing file (successive byte
    prefixes of the complete file, ticked by hand rather than through the
    real background thread/timer) and checks both channels show up, in time
    order. Complements TestRealLocalProvider's batch-mode coverage of the
    same fixture."""

    def setUp(self):
        if not _has("ffmpeg", "ffprobe", "voxtype"):
            self.skipTest("ffmpeg/ffprobe/voxtype not available")

    def test_both_channels_appear_in_order_as_the_file_grows(self):
        sample = two_channel_sample()
        data = sample.read_bytes()
        total = len(data)
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            call_dir = tmp / "call"
            call_dir.mkdir()
            growing = tmp / "audio.opus"
            with isolated_runtime(tmp):
                cfg = {"live_transcript": True, "live_max_window_s": 12}
                lt = live.LiveTranscriber(call_dir, growing, time.time(), cfg)
                with tempfile.TemporaryDirectory() as scratch:
                    scratch_dir = Path(scratch)
                    for frac in (0.25, 0.5, 0.75, 1.0):
                        growing.write_bytes(data[:max(1, int(total * frac))])
                        lt._tick(scratch_dir, final=(frac >= 1.0))

        utts = sorted(lt._utterances, key=lambda u: u["start"])
        self.assertTrue(utts, "expected at least some transcribed speech")
        channels = {u["channel"] for u in utts}
        self.assertEqual(channels, {0, 1}, "expected speech detected on both channels")
        for u in utts:
            self.assertTrue(u["transcript"].strip(), f"empty transcript on channel {u['channel']}")
        # channel 0 (spacewalk.wav) starts at 0s; channel 1 (bueller.wav) is
        # delayed a few seconds -- ordered by time, channel 0 leads.
        self.assertEqual(utts[0]["channel"], 0)
        self.assertIn(1, [u["channel"] for u in utts])


if __name__ == "__main__":
    unittest.main()
