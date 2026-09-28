"""Live tests: exercise real PipeWire, real ffmpeg, real Deepgram, and the
real local summary model configured in the live user's own
~/.config/spitball/config.json. Only runs with `tests/run.sh --live` or
SPITBALL_LIVE=1 -- skipped entirely otherwise, including in every offline
run and in CI.

Never touches the live daemon: it reads the live config file read-only (for
credentials only, via config.secret()) and never opens the live runtime
socket, state file, or ~/Calls. Every recording/process here writes into its
own tempfile.TemporaryDirectory().
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from spitball import config, process
from spitball.recorder import Recording
from spitball import detect
from tests.live.live_fixtures import two_channel_sample

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
        dg = process.transcribe(audio, cfg)
        dg_cache = audio.with_name("two_channel_sample.deepgram.json")
        dg_cache.write_text(json.dumps(dg))  # reused by the full-process test below

        utterances = dg.get("results", {}).get("utterances", [])
        self.assertTrue(utterances, "Deepgram returned no utterances at all")
        channels = {u["channel"] for u in utterances}
        self.assertEqual(channels, {0, 1}, "expected speech detected on both channels")

        lines = process.build_transcript(dg, cfg)
        speakers = {w for _, w, _ in lines}
        self.assertIn(cfg["my_name"], speakers)
        self.assertIn("Them", speakers)


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
            cfg["deepgram_api_key"] = key
            cfg["export_dir"] = ""  # explicitly off

            result = process.process(call_dir, meta, cfg)

            final_dir = Path(result["dir"])
            self.assertTrue((final_dir / "audio.opus").exists())
            self.assertTrue((final_dir / "transcript.md").exists())
            summary_text = Path(result["summary"]).read_text()
            self.assertTrue(summary_text.strip().startswith("#"), summary_text[:200])


if __name__ == "__main__":
    unittest.main()
