import http.server
import io
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from spitball import config, process
from spitball.providers import deepgram
from tests.testutil import load_fixture, make_cfg


def _normalized_fixture(name: str, cfg: dict) -> dict:
    """The fixtures under tests/fixtures/ are Deepgram's own raw JSON shape
    (results.utterances[...]) -- real API responses, still used to test
    deepgram.normalize() directly in test_provider_deepgram.py. Everything
    downstream of a provider (build_transcript, the process() pipeline) now
    works on the shared normalized shape, so tests exercising that stage
    normalize the fixture first, same as the real pipeline does."""
    return deepgram.normalize(load_fixture(name), cfg)


class TestHms(unittest.TestCase):
    def test_under_a_minute(self):
        self.assertEqual(process._hms(5), "00:00:05")

    def test_minutes(self):
        self.assertEqual(process._hms(65), "00:01:05")

    def test_hours(self):
        self.assertEqual(process._hms(3725), "01:02:05")

    def test_truncates_fractional_seconds(self):
        self.assertEqual(process._hms(5.9), "00:00:05")


class TestSlug(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(process._slug("Weekly Sync with Morgan"), "weekly-sync-with-morgan")

    def test_punctuation_collapses(self):
        self.assertEqual(process._slug("Q3!! Review -- Numbers??"), "q3-review-numbers")

    def test_limit(self):
        s = process._slug("word " * 40, limit=20)
        self.assertLessEqual(len(s), 20)
        self.assertFalse(s.endswith("-"))

    def test_empty_falls_back_to_call(self):
        self.assertEqual(process._slug("???"), "call")
        self.assertEqual(process._slug(""), "call")


class TestNewCallDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = make_cfg(Path(self.tmp.name))

    def test_creates_dir_with_stamp_and_app(self):
        d = process.new_call_dir(self.cfg, "Zoom")
        self.assertTrue(d.is_dir())
        stamp = time.strftime("%Y-%m-%d-%H%M")
        self.assertEqual(d.name, f"{stamp}-zoom")

    def test_manual_app_slug(self):
        d = process.new_call_dir(self.cfg, "")
        self.assertTrue(d.name.endswith("-manual"))

    def test_collision_gets_suffix(self):
        d1 = process.new_call_dir(self.cfg, "Zoom")
        d2 = process.new_call_dir(self.cfg, "Zoom")
        d3 = process.new_call_dir(self.cfg, "Zoom")
        self.assertNotEqual(d1, d2)
        self.assertNotEqual(d2, d3)
        self.assertTrue(d2.name.endswith("-2"))
        self.assertTrue(d3.name.endswith("-3"))


class TestAudioSeconds(unittest.TestCase):
    def test_reads_real_duration_via_ffprobe(self):
        # A tiny real silent file -- exercises the real ffprobe subprocess call
        # (offline: no network, just the system tool recorder.py already
        # depends on), not just the parsing logic.
        import shutil
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self.skipTest("ffmpeg/ffprobe not installed")
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "audio.opus"
            os.system(f"ffmpeg -hide_banner -loglevel error -f lavfi -i anullsrc "
                      f"-t 1.5 -c:a libopus {audio}")
            secs = process.audio_seconds(audio)
        self.assertAlmostEqual(secs, 1.5, delta=0.3)

    def test_missing_file_returns_zero(self):
        self.assertEqual(process.audio_seconds(Path("/no/such/file.opus")), 0.0)

    def test_ffprobe_failure_returns_zero(self):
        with mock.patch("subprocess.run", side_effect=OSError("no ffprobe")):
            self.assertEqual(process.audio_seconds(Path("/tmp/x.opus")), 0.0)

    def test_garbage_output_returns_zero(self):
        result = mock.Mock()
        result.stdout = "not-a-number\n"
        with mock.patch("subprocess.run", return_value=result):
            self.assertEqual(process.audio_seconds(Path("/tmp/x.opus")), 0.0)


class TestBuildTranscript(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = make_cfg(Path(self.tmp.name), my_name="Morgan")

    def test_single_far_speaker_echo_and_merge(self):
        dg = _normalized_fixture("deepgram_single_speaker.json", self.cfg)
        lines = process.build_transcript(dg, self.cfg)
        speakers_texts = [(round(s, 1), w, t) for s, w, t in lines]
        self.assertEqual(speakers_texts, [
            (0.0, "Them", "Hey how's it going today"),
            (2.5, "Morgan", "I'm doing great thanks and glad to be here"),
            (20.0, "Them", "Quick question for you about the deadline"),
            (40.0, "Morgan", "Sure, let's talk about it"),
        ])

    def test_echo_utterance_is_dropped(self):
        dg = _normalized_fixture("deepgram_single_speaker.json", self.cfg)
        lines = process.build_transcript(dg, self.cfg)
        full_text = " ".join(t for _, _, t in lines)
        self.assertNotIn("hey how's it going today and", full_text)  # the echoed dup
        self.assertEqual(sum(1 for _, w, t in lines if "Hey how's it going today" in t), 1)

    def test_real_mic_speech_kept_when_not_an_echo(self):
        dg = {"utterances": [
            {"channel": 1, "start": 0.0, "end": 1.5, "speaker": 0,
             "transcript": "so what did you think of the proposal"},
            {"channel": 0, "start": 6.0, "end": 8.0, "speaker": 0,
             "transcript": "honestly I think we should rewrite the whole thing"},
        ]}
        lines = process.build_transcript(dg, self.cfg)
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[1][1], "Morgan")
        self.assertIn("rewrite the whole thing", lines[1][2])

    def test_multi_far_speakers_labeled(self):
        dg = _normalized_fixture("deepgram_multi_speaker.json", self.cfg)
        lines = process.build_transcript(dg, self.cfg)
        labels = [w for _, w, _ in lines]
        self.assertEqual(labels, ["Speaker 1", "Speaker 2", "Morgan"])

    def test_no_speech_returns_empty(self):
        dg = _normalized_fixture("deepgram_no_speech.json", self.cfg)
        lines = process.build_transcript(dg, self.cfg)
        self.assertEqual(lines, [])

    def test_blank_transcript_utterances_filtered(self):
        dg = {"utterances": [
            {"channel": 0, "start": 0.0, "end": 1.0, "speaker": 0, "transcript": "   "},
            {"channel": 0, "start": 2.0, "end": 3.0, "speaker": 0, "transcript": "real words"},
        ]}
        lines = process.build_transcript(dg, self.cfg)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0][2], "real words")

    def test_consecutive_same_speaker_merged_within_4s(self):
        dg = {"utterances": [
            {"channel": 1, "start": 0.0, "end": 1.0, "speaker": 0, "transcript": "part one"},
            {"channel": 1, "start": 2.0, "end": 3.0, "speaker": 0, "transcript": "part two"},
        ]}
        lines = process.build_transcript(dg, self.cfg)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0][2], "part one part two")

    def test_gap_over_4s_not_merged(self):
        dg = {"utterances": [
            {"channel": 1, "start": 0.0, "end": 1.0, "speaker": 0, "transcript": "part one"},
            {"channel": 1, "start": 10.0, "end": 11.0, "speaker": 0, "transcript": "part two"},
        ]}
        lines = process.build_transcript(dg, self.cfg)
        self.assertEqual(len(lines), 2)


class TestTranscribe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.audio = Path(self.tmp.name) / "audio.opus"
        self.audio.write_bytes(b"fake-opus-bytes")
        self.cfg = make_cfg(Path(self.tmp.name), transcription_provider="deepgram",
                            deepgram_api_key="dg-test-key-123")
        self._env_patch = mock.patch.dict(os.environ, {}, clear=False)
        self._env_patch.start()
        os.environ.pop("DEEPGRAM_API_KEY", None)
        self.addCleanup(self._env_patch.stop)

    def test_builds_request_url_and_auth_header(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["req"] = req
            captured["timeout"] = timeout
            return io.BytesIO(json.dumps({"results": {"utterances": []}}).encode())

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = process.transcribe(self.audio, self.cfg)

        req = captured["req"]
        self.assertTrue(req.full_url.startswith(process.DEEPGRAM_URL))
        for expected in ("model=nova-3", "multichannel=true", "diarize=true",
                         "utterances=true", "language=en"):
            self.assertIn(expected, req.full_url)
        self.assertEqual(req.get_header("Authorization"), "Token dg-test-key-123")
        self.assertEqual(req.data, b"fake-opus-bytes")
        # process.transcribe() dispatches through the provider abstraction now,
        # so it returns the normalized shape, not Deepgram's raw JSON.
        self.assertEqual(result, {"provider": "deepgram", "model": "nova-3", "utterances": []})

    def test_env_var_overrides_cfg_key(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["req"] = req
            return io.BytesIO(json.dumps({"results": {"utterances": []}}).encode())

        with mock.patch.dict(os.environ, {"DEEPGRAM_API_KEY": "env-key"}), \
             mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            process.transcribe(self.audio, self.cfg)
        self.assertEqual(captured["req"].get_header("Authorization"), "Token env-key")

    def test_missing_key_raises_clear_error(self):
        cfg = make_cfg(Path(self.tmp.name), transcription_provider="deepgram")  # key/_command both empty
        with self.assertRaises(RuntimeError) as cm:
            process.transcribe(self.audio, cfg)
        self.assertIn("No Deepgram API key", str(cm.exception))
        self.assertIn("DEEPGRAM_API_KEY", str(cm.exception))

    def test_http_error_surfaces_clear_message(self):
        def raise_http_error(req, timeout=None):
            raise urllib.error.HTTPError(process.DEEPGRAM_URL, 401, "Unauthorized",
                                          None, io.BytesIO(b"bad key"))

        with mock.patch("urllib.request.urlopen", side_effect=raise_http_error):
            with self.assertRaises(RuntimeError) as cm:
                process.transcribe(self.audio, self.cfg)
        self.assertIn("401", str(cm.exception))

    def test_connection_error_surfaces_clear_message(self):
        def raise_url_error(req, timeout=None):
            raise urllib.error.URLError("network unreachable")

        with mock.patch("urllib.request.urlopen", side_effect=raise_url_error):
            with self.assertRaises(RuntimeError) as cm:
                process.transcribe(self.audio, self.cfg)
        self.assertIn("unreachable", str(cm.exception))


# ---------------------------------------------------------------------------
# A minimal OpenAI-compatible fake server, for summarize() request-shape tests.

class _FakeOpenAIHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a, **kw):
        pass

    def _reply(self, status, payload):
        data = payload if isinstance(payload, (bytes, bytearray)) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.server.requests.append({"method": "GET", "path": self.path,
                                      "headers": dict(self.headers), "body": None})
        status, payload = self.server.behavior("GET", self.path, None)
        self._reply(status, payload)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = None
        self.server.requests.append({"method": "POST", "path": self.path,
                                      "headers": dict(self.headers), "body": body})
        status, payload = self.server.behavior("POST", self.path, body)
        self._reply(status, payload)


class FakeOpenAIServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_fake_server(behavior):
    """behavior(method, path, body) -> (status, payload). payload is a dict
    (JSON-encoded) or raw bytes (for the garbage-reply case)."""
    srv = FakeOpenAIServer(("127.0.0.1", 0), _FakeOpenAIHandler)
    srv.requests = []
    srv.behavior = behavior
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return srv, thread


def stop_fake_server(srv, thread):
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)


def _default_behavior(model_id="test-model", content="# A Title\n\n## Summary\n- talked"):
    def behavior(method, path, body):
        if path.endswith("/models"):
            return 200, {"data": [{"id": model_id}]}
        if path.endswith("/chat/completions"):
            return 200, {"choices": [{"message": {"content": content}}]}
        return 404, {"error": "not found"}
    return behavior


class TestSummarize(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _cfg(self, **overrides):
        cfg = make_cfg(Path(self.tmp.name))
        cfg.update(overrides)
        return cfg

    def _server(self, behavior):
        srv, thread = start_fake_server(behavior)
        self.addCleanup(stop_fake_server, srv, thread)
        return srv

    def test_uses_first_model_when_none_configured(self):
        srv = self._server(_default_behavior(model_id="picked-model"))
        cfg = self._cfg(summary_base_url=f"http://127.0.0.1:{srv.server_address[1]}/v1",
                         summary_model="")
        process.summarize("hello world", "meta", cfg)
        get_reqs = [r for r in srv.requests if r["method"] == "GET"]
        post_reqs = [r for r in srv.requests if r["method"] == "POST"]
        self.assertEqual(len(get_reqs), 1)
        self.assertTrue(get_reqs[0]["path"].endswith("/models"))
        self.assertEqual(post_reqs[0]["body"]["model"], "picked-model")

    def test_configured_model_skips_models_lookup(self):
        srv = self._server(_default_behavior())
        cfg = self._cfg(summary_base_url=f"http://127.0.0.1:{srv.server_address[1]}/v1",
                         summary_model="fixed-model")
        process.summarize("hello world", "meta", cfg)
        self.assertEqual([r["method"] for r in srv.requests], ["POST"])
        self.assertEqual(srv.requests[0]["body"]["model"], "fixed-model")

    def test_request_shape(self):
        srv = self._server(_default_behavior())
        cfg = self._cfg(summary_base_url=f"http://127.0.0.1:{srv.server_address[1]}/v1",
                         summary_model="fixed-model", my_name="Morgan")
        process.summarize("the transcript body", "**Date:** today", cfg)
        body = srv.requests[-1]["body"]
        self.assertEqual(body["temperature"], 0.3)
        self.assertEqual(body["max_tokens"], 4000)
        messages = body["messages"]
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("Morgan", messages[0]["content"])
        self.assertEqual(messages[1]["role"], "user")
        self.assertIn("the transcript body", messages[1]["content"])
        self.assertIn("**Date:** today", messages[1]["content"])

    def test_auth_header_present_when_key_set(self):
        srv = self._server(_default_behavior())
        cfg = self._cfg(summary_base_url=f"http://127.0.0.1:{srv.server_address[1]}/v1",
                         summary_model="m", summary_api_key="sekret")
        process.summarize("t", "m", cfg)
        self.assertEqual(srv.requests[-1]["headers"].get("Authorization"), "Bearer sekret")

    def test_auth_header_absent_when_no_key(self):
        srv = self._server(_default_behavior())
        cfg = self._cfg(summary_base_url=f"http://127.0.0.1:{srv.server_address[1]}/v1",
                         summary_model="m", summary_api_key="")
        process.summarize("t", "m", cfg)
        self.assertNotIn("Authorization", srv.requests[-1]["headers"])

    def test_auth_key_via_command(self):
        srv = self._server(_default_behavior())
        cfg = self._cfg(summary_base_url=f"http://127.0.0.1:{srv.server_address[1]}/v1",
                         summary_model="m", summary_api_key="", summary_api_key_command="echo from-cmd")
        process.summarize("t", "m", cfg)
        self.assertEqual(srv.requests[-1]["headers"].get("Authorization"), "Bearer from-cmd")

    def test_think_block_is_stripped(self):
        content = "<think>reasoning I shouldn't show</think># Real Title\n\nBody text"
        srv = self._server(_default_behavior(content=content))
        cfg = self._cfg(summary_base_url=f"http://127.0.0.1:{srv.server_address[1]}/v1",
                         summary_model="m")
        out = process.summarize("t", "m", cfg)
        self.assertEqual(out, "# Real Title\n\nBody text")
        self.assertNotIn("reasoning", out)

    def test_truncates_very_long_transcript(self):
        captured = {}

        def behavior(method, path, body):
            if path.endswith("/chat/completions"):
                captured["body"] = body
                return 200, {"choices": [{"message": {"content": "# T\n\nbody"}}]}
            return 404, {}

        srv = self._server(behavior)
        cfg = self._cfg(summary_base_url=f"http://127.0.0.1:{srv.server_address[1]}/v1",
                         summary_model="m")
        long_text = "x" * (process.MAX_TRANSCRIPT_CHARS + 500)
        process.summarize(long_text, "meta", cfg)
        sent = captured["body"]["messages"][1]["content"]
        self.assertIn("[transcript truncated]", sent)
        self.assertLess(len(sent), len(long_text))

    def test_summary_disabled_raises_without_network(self):
        cfg = self._cfg(summary_enabled=False, summary_base_url="http://127.0.0.1:1")
        with self.assertRaises(RuntimeError) as cm:
            process.summarize("t", "m", cfg)
        self.assertIn("summary_enabled", str(cm.exception))

    def test_http_error_path(self):
        def behavior(method, path, body):
            return 500, {"error": "boom"}
        srv = self._server(behavior)
        cfg = self._cfg(summary_base_url=f"http://127.0.0.1:{srv.server_address[1]}/v1",
                         summary_model="m")
        with self.assertRaises(RuntimeError) as cm:
            process.summarize("t", "m", cfg)
        self.assertIn("500", str(cm.exception))

    def test_unreachable_path(self):
        # A closed local port: nothing is listening, so the connection is refused.
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        cfg = self._cfg(summary_base_url=f"http://127.0.0.1:{port}/v1", summary_model="m")
        with self.assertRaises(RuntimeError) as cm:
            process.summarize("t", "m", cfg)
        self.assertIn("unreachable", str(cm.exception))

    def test_garbage_reply_raises_clear_error(self):
        def behavior(method, path, body):
            return 200, b"not json at all"
        srv = self._server(behavior)
        cfg = self._cfg(summary_base_url=f"http://127.0.0.1:{srv.server_address[1]}/v1",
                         summary_model="m")
        with self.assertRaises(RuntimeError) as cm:
            process.summarize("t", "m", cfg)
        self.assertIn("unexpected reply", str(cm.exception))

    def test_empty_text_raises_clear_error(self):
        def behavior(method, path, body):
            if path.endswith("/chat/completions"):
                return 200, {"choices": [{"message": {"content": ""}}]}
            return 404, {}
        srv = self._server(behavior)
        cfg = self._cfg(summary_base_url=f"http://127.0.0.1:{srv.server_address[1]}/v1",
                         summary_model="m")
        with self.assertRaises(RuntimeError) as cm:
            process.summarize("t", "m", cfg)
        self.assertIn("no text", str(cm.exception))


class TestProcessPipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)
        self.cfg = make_cfg(self.tmp_path)
        self.call_dir = Path(self.cfg["calls_dir"]) / "2026-09-28-1400-zoom"
        self.call_dir.mkdir(parents=True)
        (self.call_dir / "audio.opus").write_bytes(b"fake")
        self.meta = {"app": "Zoom", "started_at": 1790000000, "duration": 125.0}
        self.dg_fixture = _normalized_fixture("deepgram_single_speaker.json", self.cfg)

    def _patched(self, summarize_return="# Weekly Sync With Morgan\n\n## Summary\n- talked",
                 transcribe_return=None):
        transcribe_return = transcribe_return if transcribe_return is not None else self.dg_fixture
        return mock.patch("spitball.process.transcribe", return_value=transcribe_return), \
            mock.patch("spitball.process.summarize", return_value=summarize_return)

    def test_visible_files_are_exactly_the_three(self):
        p_transcribe, p_summarize = self._patched()
        with p_transcribe, p_summarize:
            result = process.process(self.call_dir, self.meta, self.cfg)
        final_dir = Path(result["dir"])
        visible = {p.name for p in final_dir.iterdir() if not p.name.startswith(".")}
        self.assertEqual(visible, {"audio.opus", "transcript.md", "summary.md"})

    def test_title_parsed_from_summary_heading(self):
        p_transcribe, p_summarize = self._patched()
        with p_transcribe, p_summarize:
            result = process.process(self.call_dir, self.meta, self.cfg)
        self.assertEqual(result["title"], "Weekly Sync With Morgan")

    def test_folder_renamed_with_slug_once(self):
        p_transcribe, p_summarize = self._patched()
        with p_transcribe, p_summarize:
            result = process.process(self.call_dir, self.meta, self.cfg)
        final_dir = Path(result["dir"])
        self.assertTrue(final_dir.name.endswith("-weekly-sync-with-morgan"))
        self.assertFalse(self.call_dir.exists())

    def test_reprocess_does_not_double_rename(self):
        p_transcribe, p_summarize = self._patched()
        with p_transcribe, p_summarize:
            result = process.process(self.call_dir, self.meta, self.cfg)
        final_dir = Path(result["dir"])
        p_transcribe2, p_summarize2 = self._patched(
            summarize_return="# Weekly Sync With Morgan\n\n## Summary\n- talked more")
        with p_transcribe2 as t2, p_summarize2:
            result2 = process.process(final_dir, {}, self.cfg)
        t2.assert_not_called()  # cached .transcript.json reused
        self.assertEqual(result2["dir"], result["dir"])

    def test_reprocess_reuses_cached_transcript_json(self):
        p_transcribe, p_summarize = self._patched()
        with p_transcribe as t1, p_summarize:
            process.process(self.call_dir, self.meta, self.cfg)
        self.assertEqual(t1.call_count, 1)
        final_dir = next(iter(Path(self.cfg["calls_dir"]).glob("*")))
        self.assertTrue((final_dir / ".transcript.json").exists())
        with mock.patch("spitball.process.transcribe") as t2, \
             mock.patch("spitball.process.summarize", return_value="# T2\n\nx"):
            process.process(final_dir, {}, self.cfg)
        t2.assert_not_called()

    def test_retranscribe_ignores_cache(self):
        p_transcribe, p_summarize = self._patched()
        with p_transcribe as t1, p_summarize:
            process.process(self.call_dir, self.meta, self.cfg)
        self.assertEqual(t1.call_count, 1)
        final_dir = next(iter(Path(self.cfg["calls_dir"]).glob("*")))
        with mock.patch("spitball.process.transcribe", return_value=self.dg_fixture) as t2, \
             mock.patch("spitball.process.summarize", return_value="# T2\n\nx"):
            process.process(final_dir, {}, self.cfg, retranscribe=True)
        t2.assert_called_once()  # --retranscribe skips the cache and calls the provider again

    def test_legacy_deepgram_json_cache_is_read_and_normalized(self):
        # A call folder made before .transcript.json existed only has the
        # old .deepgram.json (Deepgram's raw shape) -- reprocessing it must
        # not need a network call, and must still produce the real transcript.
        (self.call_dir / ".deepgram.json").write_text(json.dumps(load_fixture("deepgram_single_speaker.json")))
        with mock.patch("spitball.process.transcribe") as t, \
             mock.patch("spitball.process.summarize", return_value="# T\n\nx") as sm:
            result = process.process(self.call_dir, self.meta, self.cfg)
        t.assert_not_called()
        sm.assert_called_once()
        transcript_text = Path(result["dir"], "transcript.md").read_text()
        self.assertIn("Hey how's it going today", transcript_text)
        # The pipeline also wrote the new-format cache so future reprocesses
        # don't need to re-read/re-normalize the legacy file.
        self.assertTrue(Path(result["dir"], ".transcript.json").exists())

    def test_export_dir_gets_summary_and_transcript_never_audio(self):
        cfg = dict(self.cfg)
        cfg["export_dir"] = str(self.tmp_path / "export")
        p_transcribe, p_summarize = self._patched()
        with p_transcribe, p_summarize:
            result = process.process(self.call_dir, self.meta, cfg)
        export_files = list(Path(cfg["export_dir"]).glob("*"))
        self.assertEqual(len(export_files), 1)
        self.assertTrue(export_files[0].name.endswith(".md"))
        self.assertIn(Path(result["dir"]).name, export_files[0].name)
        exported_text = export_files[0].read_text()
        self.assertIn("Weekly Sync With Morgan", exported_text)
        self.assertIn("Hey how's it going today", exported_text)  # transcript included
        self.assertNotIn("audio.opus", "".join(p.suffix for p in export_files))

    def test_export_dir_off_writes_nothing(self):
        cfg = dict(self.cfg)
        cfg["export_dir"] = ""
        p_transcribe, p_summarize = self._patched()
        with p_transcribe, p_summarize:
            process.process(self.call_dir, self.meta, cfg)
        # No export dir was ever created since it was never configured.
        self.assertFalse((self.tmp_path / "export").exists())

    def test_llm_failure_falls_back_with_reprocess_hint(self):
        p_transcribe, _ = self._patched()
        with p_transcribe, mock.patch("spitball.process.summarize",
                                       side_effect=RuntimeError("model unreachable")):
            result = process.process(self.call_dir, self.meta, self.cfg)
        summary_text = Path(result["summary"]).read_text()
        self.assertIn("model unreachable", summary_text)
        self.assertIn("spitball reprocess", summary_text)

    def test_no_speech_path(self):
        p_transcribe, p_summarize = self._patched(
            transcribe_return=_normalized_fixture("deepgram_no_speech.json", self.cfg))
        with p_transcribe, p_summarize as sm:
            result = process.process(self.call_dir, self.meta, self.cfg)
        sm.assert_not_called()  # no lines -> summarize() is never called
        transcript_text = (Path(result["dir"]) / "transcript.md").read_text()
        self.assertIn("No speech detected", transcript_text)
        summary_text = Path(result["summary"]).read_text()
        self.assertIn("No speech was detected", summary_text)

    def test_deepgram_http_error_surfaces(self):
        with mock.patch("spitball.process.transcribe",
                         side_effect=RuntimeError("Deepgram error 401: bad key")):
            with self.assertRaises(RuntimeError) as cm:
                process.process(self.call_dir, self.meta, self.cfg)
        self.assertIn("401", str(cm.exception))

    def test_notify_called_with_progress_messages(self):
        p_transcribe, p_summarize = self._patched()
        seen = []
        with p_transcribe, p_summarize:
            process.process(self.call_dir, self.meta, self.cfg, notify=seen.append)
        self.assertIn("Transcribing…", seen)
        self.assertIn("Summarizing…", seen)


class TestMicDenoiseMetadata(TestProcessPipeline):
    """The provider's `mic_denoise` block (docs/SPEC-v2.md section 3) rides
    into `.transcript.json`, whether it came from a fresh transcription or a
    reused `.live.json`, and `reprocess --retranscribe` hands the provider
    the current setting."""

    BLOCK = {"mode": "auto", "applied": True, "filter": "arnndn",
             "noise_floor_db": -38.2, "threshold_db": -45.0, "speech_level_db": -21.0}

    def test_block_is_cached_in_transcript_json(self):
        normalized = dict(self.dg_fixture, mic_denoise=self.BLOCK)
        p_transcribe, p_summarize = self._patched(transcribe_return=normalized)
        with p_transcribe, p_summarize:
            result = process.process(self.call_dir, self.meta, self.cfg)
        cached = json.loads((Path(result["dir"]) / ".transcript.json").read_text())
        self.assertEqual(cached["mic_denoise"], self.BLOCK)

    def test_retranscribe_passes_the_current_setting_to_the_provider(self):
        p_transcribe, p_summarize = self._patched()
        with p_transcribe, p_summarize:
            result = process.process(self.call_dir, self.meta, self.cfg)
        final_dir = Path(result["dir"])
        cfg = dict(self.cfg, mic_denoise="on", mic_noise_floor_db=-52)
        with mock.patch("spitball.process.transcribe", return_value=self.dg_fixture) as t, \
             mock.patch("spitball.process.summarize", return_value="# T\n\n## Summary\n- x"):
            process.process(final_dir, {}, cfg, retranscribe=True)
        t.assert_called_once()
        passed_cfg = t.call_args[0][1]
        self.assertEqual(passed_cfg["mic_denoise"], "on")
        self.assertEqual(passed_cfg["mic_noise_floor_db"], -52)

    def test_live_json_block_survives_reuse(self):
        live = {"provider": "local", "model": "m", "note": "live transcript",
                "utterances": [{"channel": 0, "speaker": 0, "start": 0.0, "end": 5.0, "transcript": "hi"}],
                "mic_denoise": dict(self.BLOCK, mode="on")}
        (self.call_dir / ".live.json").write_text(json.dumps(live))
        with mock.patch("spitball.process.transcribe") as t, \
             mock.patch("spitball.process.summarize", return_value="# T\n\n## Summary\n- x"):
            result = process.process(self.call_dir, self.meta, self.cfg)
        t.assert_not_called()
        cached = json.loads((Path(result["dir"]) / ".transcript.json").read_text())
        self.assertEqual(cached["mic_denoise"]["mode"], "on")
        self.assertTrue(cached["mic_denoise"]["applied"])


if __name__ == "__main__":
    unittest.main()


class TestFailedPartsNote(TestProcessPipeline):
    def test_header_notes_parts_that_failed(self):
        normalized = {"provider": "local", "model": "x", "utterances": [
            {"channel": 0, "speaker": 0, "start": 0.0, "end": 5.0, "transcript": "hello there"},
            {"channel": 0, "speaker": 0, "start": 5.0, "end": 9.0,
             "transcript": "[transcription failed for this part]", "failed": True}]}
        p_transcribe, p_summarize = self._patched(transcribe_return=normalized)
        with p_transcribe, p_summarize:
            result = process.process(self.call_dir, self.meta, self.cfg)
        text = (Path(result["dir"]) / "transcript.md").read_text()
        self.assertIn("1 part of the audio couldn't be transcribed", text)
        self.assertIn("[transcription failed for this part]", text)


class TestLiveTranscriptReuse(TestProcessPipeline):
    """`.live.json` reuse rules, per docs/SPEC-live-transcript.md: process()
    uses the live transcriber's cached output instead of transcribing again,
    but only for the local provider, and only when it looks usable."""

    LIVE_JSON = {"provider": "local", "model": "base.en", "note": "live transcript", "utterances": [
        {"channel": 0, "speaker": 0, "start": 0.0, "end": 3.0, "transcript": "hey thanks for hopping on"},
        {"channel": 1, "speaker": 0, "start": 4.0, "end": 7.0, "transcript": "of course glad to help"},
    ]}

    def _write_live_json(self, data=None):
        (self.call_dir / ".live.json").write_text(json.dumps(self.LIVE_JSON if data is None else data))

    def test_used_when_provider_is_local_and_looks_clean(self):
        self._write_live_json()
        cfg = dict(self.cfg, transcription_provider="local")
        with mock.patch("spitball.process.transcribe") as t, \
             mock.patch("spitball.process.summarize", return_value="# Call\n\n## Summary\n- x"):
            result = process.process(self.call_dir, self.meta, cfg)
        t.assert_not_called()  # .live.json substituted for a real transcribe() call
        text = Path(result["summary"]).read_text()  # summary.md, not the transcript -- just proves it ran
        self.assertTrue(text)
        cached = json.loads((Path(result["dir"]) / ".transcript.json").read_text())
        self.assertEqual(cached["provider"], "local")

    def test_never_used_for_deepgram_even_if_present(self):
        self._write_live_json()
        cfg = dict(self.cfg, transcription_provider="deepgram")
        p_transcribe, p_summarize = self._patched()
        with p_transcribe as t, p_summarize:
            process.process(self.call_dir, self.meta, cfg)
        t.assert_called_once()  # full deepgram transcription still ran

    def test_missing_file_falls_back_to_full_transcription(self):
        cfg = dict(self.cfg, transcription_provider="local")
        p_transcribe, p_summarize = self._patched(transcribe_return={
            "provider": "local", "model": "x", "utterances": []})
        with p_transcribe as t, p_summarize:
            process.process(self.call_dir, self.meta, cfg)
        t.assert_called_once()

    def test_empty_utterances_falls_back(self):
        self._write_live_json({"provider": "local", "model": "x", "utterances": []})
        cfg = dict(self.cfg, transcription_provider="local")
        p_transcribe, p_summarize = self._patched(transcribe_return={
            "provider": "local", "model": "x", "utterances": []})
        with p_transcribe as t, p_summarize:
            process.process(self.call_dir, self.meta, cfg)
        t.assert_called_once()

    def test_unparsable_json_falls_back(self):
        (self.call_dir / ".live.json").write_text("not json")
        cfg = dict(self.cfg, transcription_provider="local")
        p_transcribe, p_summarize = self._patched(transcribe_return={
            "provider": "local", "model": "x", "utterances": []})
        with p_transcribe as t, p_summarize:
            process.process(self.call_dir, self.meta, cfg)
        t.assert_called_once()

    def test_more_than_ten_percent_failed_by_duration_falls_back(self):
        # 6s failed out of 10s total spoken time -> well over the 10% cap.
        self._write_live_json({"provider": "local", "model": "x", "note": "live transcript", "utterances": [
            {"channel": 0, "speaker": 0, "start": 0.0, "end": 4.0, "transcript": "ok"},
            {"channel": 0, "speaker": 0, "start": 4.0, "end": 10.0,
             "transcript": "[transcription failed for this part]", "failed": True},
        ]})
        cfg = dict(self.cfg, transcription_provider="local")
        p_transcribe, p_summarize = self._patched(transcribe_return={
            "provider": "local", "model": "x", "utterances": []})
        with p_transcribe as t, p_summarize:
            process.process(self.call_dir, self.meta, cfg)
        t.assert_called_once()

    def test_a_few_failures_under_ten_percent_still_used(self):
        # 1s failed out of 10s total -> under the 10% cap, still reused.
        self._write_live_json({"provider": "local", "model": "x", "note": "live transcript", "utterances": [
            {"channel": 0, "speaker": 0, "start": 0.0, "end": 9.0, "transcript": "ok"},
            {"channel": 0, "speaker": 0, "start": 9.0, "end": 10.0,
             "transcript": "[transcription failed for this part]", "failed": True},
        ]})
        cfg = dict(self.cfg, transcription_provider="local")
        with mock.patch("spitball.process.transcribe") as t, \
             mock.patch("spitball.process.summarize", return_value="# Call\n\n## Summary\n- x"):
            process.process(self.call_dir, self.meta, cfg)
        t.assert_not_called()

    def test_retranscribe_ignores_live_json_too(self):
        self._write_live_json()
        cfg = dict(self.cfg, transcription_provider="local")
        p_transcribe, p_summarize = self._patched(transcribe_return={
            "provider": "local", "model": "x", "utterances": []})
        with p_transcribe as t, p_summarize:
            process.process(self.call_dir, self.meta, cfg, retranscribe=True)
        t.assert_called_once()


class TestCalendarInPipeline(TestProcessPipeline):
    """The calendar's hooks in process() (docs/SPEC-v2.md §2): a confident
    match names the folder after the event, heads transcript.md/summary.md
    with the meeting + attendees, stores `meeting` in .transcript.json, and
    feeds the invite list to the summarizer; a weak match changes nothing
    but a header line; off means byte-for-byte the old behavior."""

    T = 1790000000  # the pipeline's started_at; events are placed relative to it

    def _event(self, title="Weekly sync", offset_s=-120, minutes=30, attendees=2, **extra):
        from datetime import datetime, timedelta, timezone
        from spitball import calendar as cal
        s = datetime.fromtimestamp(self.T + offset_s, tz=timezone.utc)
        people = [{"name": f"Person {i}", "email": f"p{i}@example.com", "response": "accepted",
                   "self": False, "optional": False} for i in range(attendees)]
        people.append({"name": "", "email": "owner@example.com", "response": "accepted", "self": True,
                       "optional": False})
        ev = {"id": f"{title}@x", "uid": title, "title": title, "start": cal.to_iso(s),
              "end": cal.to_iso(s + timedelta(minutes=minutes)), "all_day": False, "status": "confirmed",
              "transparency": "opaque", "kind": "default", "my_response": "accepted",
              "organizer": {"name": "Person 0", "email": "p0@example.com"}, "attendees": people,
              "conference": {"kind": "meet", "url": "https://meet.google.com/abc-defg-hij", "code": "abc-defg-hij"},
              "location": "", "description": "Agenda: numbers", "recurring": False, "recurrence_id": ""}
        ev.update(extra)
        return ev

    def _meta(self, events, meet_codes=(), **over):
        meta = dict(self.meta)
        meta["calendar"] = {"source": "ics", "fetched_at": 1, "cached": False, "error": "", "app": "Chrome",
                            "started_at": self.T, "meet_codes": list(meet_codes), "events": events, "match": None}
        meta.update(over)
        return meta

    def test_confident_match_names_folder_headers_and_transcript_json(self):
        cfg = dict(self.cfg, calendar_enabled=True)
        p_transcribe, p_summarize = self._patched()
        with p_transcribe, p_summarize as summ:
            result = process.process(self.call_dir, self._meta([self._event()], ["abc-defg-hij"]), cfg)
        final_dir = Path(result["dir"])
        self.assertTrue(final_dir.name.endswith("-zoom-weekly-sync"), final_dir.name)
        self.assertEqual(result["title"], "Weekly sync")
        transcript = (final_dir / "transcript.md").read_text()
        self.assertTrue(transcript.startswith("# Weekly sync: transcript\n"))
        self.assertIn("**Meeting:** Weekly sync  \n**When:** ", transcript)
        self.assertIn("**Attendees:** Person 0 (organizer), Person 1", transcript)
        summary = (final_dir / "summary.md").read_text()
        self.assertTrue(summary.startswith("# Weekly sync\n\n## Summary\n- talked"), summary[:80])
        self.assertNotIn("Weekly Sync With Morgan", summary)  # the model's heading is replaced
        self.assertIn("**Meeting:** Weekly sync", summary)
        meta_text = summ.call_args.args[1]
        self.assertIn("**People on the invite:** Person 0 (organizer), Person 1", meta_text)
        self.assertNotIn("Agenda: numbers", meta_text)  # description off by default
        cache = json.loads((final_dir / ".transcript.json").read_text())
        self.assertEqual(cache["meeting"]["title"], "Weekly sync")
        self.assertEqual([a["name"] for a in cache["meeting"]["attendees"]], ["Person 0", "Person 1", ""])
        self.assertTrue(cache["meeting"]["attendees"][2]["self"])
        saved = json.loads((final_dir / ".meta.json").read_text())
        self.assertEqual(saved["calendar"]["match"]["title"], "Weekly sync")
        self.assertTrue(saved["titled"])

    def test_prefer_event_title_off_keeps_model_title_but_adds_header(self):
        cfg = dict(self.cfg, calendar_enabled=True, calendar_prefer_event_title=False)
        p_transcribe, p_summarize = self._patched()
        with p_transcribe, p_summarize:
            result = process.process(self.call_dir, self._meta([self._event()], ["abc-defg-hij"]), cfg)
        final_dir = Path(result["dir"])
        self.assertTrue(final_dir.name.endswith("-weekly-sync-with-morgan"))
        self.assertEqual(result["title"], "Weekly Sync With Morgan")
        self.assertIn("**Meeting:** Weekly sync", (final_dir / "transcript.md").read_text())
        self.assertIn("meeting", json.loads((final_dir / ".transcript.json").read_text()))

    def test_weak_match_never_renames_and_says_so(self):
        cfg = dict(self.cfg, calendar_enabled=True)
        events = [self._event("Weekly sync"), self._event("Design review")]  # a tie
        p_transcribe, p_summarize = self._patched()
        with p_transcribe, p_summarize as summ:
            result = process.process(self.call_dir, self._meta(events), cfg)
        final_dir = Path(result["dir"])
        self.assertTrue(final_dir.name.endswith("-weekly-sync-with-morgan"))
        transcript = (final_dir / "transcript.md").read_text()
        self.assertIn("**Calendar:** no confident match (2 candidates)", transcript)
        self.assertNotIn("**Meeting:**", transcript)
        self.assertNotIn("People on the invite", summ.call_args.args[1])
        self.assertNotIn("meeting", json.loads((final_dir / ".transcript.json").read_text()))
        self.assertFalse(json.loads((final_dir / ".meta.json").read_text())["calendar"]["match"]["confident"])

    def test_description_sent_only_when_enabled(self):
        cfg = dict(self.cfg, calendar_enabled=True, calendar_description_to_summary=True)
        p_transcribe, p_summarize = self._patched()
        with p_transcribe, p_summarize as summ:
            process.process(self.call_dir, self._meta([self._event()], ["abc-defg-hij"]), cfg)
        self.assertIn("**Event description:**\nAgenda: numbers", summ.call_args.args[1])

    def test_no_snapshot_and_calendar_off_is_unchanged(self):
        p_transcribe, p_summarize = self._patched()
        with p_transcribe, p_summarize as summ:
            result = process.process(self.call_dir, self.meta, self.cfg)
        final_dir = Path(result["dir"])
        self.assertNotIn("Calendar", (final_dir / "transcript.md").read_text())
        self.assertNotIn("Meeting", summ.call_args.args[1])
        self.assertNotIn("calendar", json.loads((final_dir / ".meta.json").read_text()))
        self.assertNotIn("meeting", json.loads((final_dir / ".transcript.json").read_text()))

    def test_no_snapshot_but_enabled_looks_up_now(self):
        from spitball import calendar as cal
        cfg = dict(self.cfg, calendar_enabled=True, calendar_source="command",
                   calendar_command="echo '[]'")
        p_transcribe, p_summarize = self._patched()
        with p_transcribe, p_summarize, mock.patch("spitball.calendar.snapshot", wraps=cal.snapshot) as snap:
            result = process.process(self.call_dir, self.meta, cfg)
        snap.assert_called_once()
        saved = json.loads((Path(result["dir"]) / ".meta.json").read_text())
        self.assertEqual(saved["calendar"]["source"], "command")
        self.assertEqual(saved["calendar"]["events"], [])

    def test_reprocess_keeps_the_snapshot_and_override_pins_the_event(self):
        cfg = dict(self.cfg, calendar_enabled=True)
        events = [self._event("Weekly sync"), self._event("Design review")]
        p_transcribe, p_summarize = self._patched()
        with p_transcribe, p_summarize:
            result = process.process(self.call_dir, self._meta(events), cfg)
        final_dir = Path(result["dir"])
        process.set_calendar_override(final_dir, "Design review@x")
        p_transcribe2, p_summarize2 = self._patched()
        with p_transcribe2 as t2, p_summarize2:
            result2 = process.process(final_dir, {}, cfg)
        t2.assert_not_called()
        self.assertEqual(result2["dir"], str(final_dir))  # renamed once, never again
        self.assertEqual(result2["title"], "Design review")
        transcript = (final_dir / "transcript.md").read_text()
        self.assertIn("**Meeting:** Design review", transcript)
        self.assertTrue(transcript.startswith("# Design review: transcript"))
        process.set_calendar_override(final_dir, None)
        p_transcribe3, p_summarize3 = self._patched()
        with p_transcribe3, p_summarize3:
            result3 = process.process(final_dir, {}, cfg)
        self.assertEqual(result3["title"], "Weekly Sync With Morgan")
        self.assertNotIn("**Meeting:**", (final_dir / "transcript.md").read_text())

    def test_calendar_snapshot_error_never_breaks_processing(self):
        cfg = dict(self.cfg, calendar_enabled=True)
        meta = self._meta([], error="calendar feed unreachable (boom)")
        meta["calendar"]["error"] = "calendar feed unreachable (boom)"
        p_transcribe, p_summarize = self._patched()
        with p_transcribe, p_summarize:
            result = process.process(self.call_dir, meta, cfg)
        self.assertEqual(result["title"], "Weekly Sync With Morgan")
        self.assertNotIn("Calendar", (Path(result["dir"]) / "transcript.md").read_text())

    def test_summary_without_heading_still_gets_event_title(self):
        cfg = dict(self.cfg, calendar_enabled=True)
        p_transcribe, p_summarize = self._patched(summarize_return="## Summary\n- no heading from the model")
        with p_transcribe, p_summarize:
            result = process.process(self.call_dir, self._meta([self._event()], ["abc-defg-hij"]), cfg)
        summary = (Path(result["dir"]) / "summary.md").read_text()
        self.assertTrue(summary.startswith("# Weekly sync\n\n## Summary\n- no heading"))


class TestSpeakersInPipeline(TestProcessPipeline):
    """Speaker naming in process() (docs/SPEC-v2.md §4): the invite list
    plus one fake model call name Deepgram's "Speaker N" labels; a 1:1
    names "Them" with no model call; user renames survive a reprocess and
    re-render the outputs without a new summary; the invite/voice count
    mismatch and a naming failure show up as header notes."""

    def _event(self, far_names):
        from datetime import datetime, timedelta, timezone
        s = datetime.fromtimestamp(1790000000 - 120, tz=timezone.utc)
        attendees = [{"name": n, "email": f"{n.split()[0].lower()}@example.com", "response": "accepted",
                      "self": False, "optional": False} for n in far_names]
        attendees.append({"name": "", "email": "me@example.com", "response": "accepted", "self": True, "optional": False})
        return {"id": "ev-1", "uid": "ev-1", "title": "Weekly sync", "start": s.isoformat(),
                "end": (s + timedelta(minutes=30)).isoformat(), "all_day": False, "status": "confirmed",
                "transparency": "opaque", "kind": "default", "my_response": "accepted", "organizer": None,
                "attendees": attendees, "conference": None, "location": "", "description": "",
                "recurring": False, "recurrence_id": ""}

    def _decision(self, far_names):
        ev = self._event(far_names)
        return {"event": ev, "confident": True, "confidence": 100, "candidates": [], "match": ev}

    def _run(self, far_names, chat_reply=None, transcribe_return=None, cfg=None, call_dir=None, retranscribe=False):
        transcribe_return = transcribe_return if transcribe_return is not None else \
            _normalized_fixture("deepgram_multi_speaker.json", self.cfg)
        chat = mock.Mock(side_effect=AssertionError("model must not be called")) if chat_reply is None \
            else mock.Mock(return_value=chat_reply)
        decision = self._decision(far_names) if far_names is not None else None
        with mock.patch("spitball.calendar.for_call", return_value=decision), \
             mock.patch("spitball.process.transcribe", return_value=transcribe_return) as t, \
             mock.patch("spitball.speakers._chat", chat), \
             mock.patch("spitball.process.summarize", return_value="# Weekly sync\n\n## Summary\n- Speaker 2 will wait.") as sm:
            result = process.process(call_dir or self.call_dir, {} if call_dir else self.meta, cfg or self.cfg,
                                     retranscribe=retranscribe)
        return result, t, chat, sm

    def test_llm_names_deepgram_speakers_and_renders_confidence(self):
        reply = json.dumps({"Speaker 1": {"name": "Priya Nair", "confidence": "high", "evidence": "0:00 intro"},
                            "Speaker 2": {"name": "Alex Demo", "confidence": "medium", "evidence": "0:05 'Alex?'"}})
        result, t, chat, sm = self._run(["Priya Nair", "Alex Demo"], chat_reply=reply)
        final_dir = Path(result["dir"])
        text = (final_dir / "transcript.md").read_text()
        self.assertIn("**[00:00:00] Priya Nair:** I think we should ship Friday", text)
        self.assertIn("**[00:00:05] Speaker 2 (probably Alex Demo):** I disagree, let's wait", text)
        self.assertIn("**Speakers:** Speaker 1 = Priya Nair, Speaker 2 (probably Alex Demo)", text)
        # The model saw neutral labels and the invite list, not the names.
        system, user, _, _ = chat.call_args[0]
        self.assertIn("**[00:00:00] Speaker 1:** I think", user)
        self.assertIn("- Priya Nair <priya@example.com>", user)
        # The summarizer got the renamed transcript and the Speakers line.
        transcript_arg, meta_arg = sm.call_args[0][0], sm.call_args[0][1]
        self.assertIn("Priya Nair:** I think", transcript_arg)
        self.assertIn("**Speakers:** Speaker 1 = Priya Nair", meta_arg)
        cache = json.loads((final_dir / ".transcript.json").read_text())
        self.assertEqual(cache["speakers"]["1"], {"id": 2, "name": "Priya Nair", "confidence": "high",
                                                  "source": "llm", "evidence": "0:00 intro"})
        self.assertEqual(cache["speakers"]["2"]["confidence"], "medium")
        self.assertEqual(t.call_args[1], {"hints": {"far_speakers": 2}})

    def test_one_to_one_names_them_without_the_model(self):
        single = _normalized_fixture("deepgram_single_speaker.json", self.cfg)
        result, _, chat, sm = self._run(["Priya Nair"], transcribe_return=single)
        text = (Path(result["dir"]) / "transcript.md").read_text()
        self.assertIn("**[00:00:00] Priya Nair:** Hey how's it going today", text)
        self.assertNotIn("Them:", text)
        self.assertIn("**Speakers:** Them = Priya Nair", text)
        chat.assert_not_called()
        cache = json.loads((Path(result["dir"]) / ".transcript.json").read_text())
        self.assertEqual(cache["speakers"]["1"]["source"], "calendar")

    def test_no_meeting_means_neutral_labels_and_no_model_call(self):
        result, _, chat, _ = self._run(None)
        text = (Path(result["dir"]) / "transcript.md").read_text()
        self.assertIn("**[00:00:00] Speaker 1:**", text)
        self.assertNotIn("**Speakers:**", text)
        chat.assert_not_called()
        cache = json.loads((Path(result["dir"]) / ".transcript.json").read_text())
        self.assertEqual([e["name"] for e in cache["speakers"].values()], ["", ""])

    def test_naming_off_skips_the_model(self):
        cfg = dict(self.cfg, speaker_names=False)
        result, _, chat, _ = self._run(["Priya Nair", "Alex Demo"], cfg=cfg)
        chat.assert_not_called()
        self.assertIn("**[00:00:00] Speaker 1:**", (Path(result["dir"]) / "transcript.md").read_text())

    def test_model_failure_keeps_neutral_labels_and_notes_it(self):
        chat = mock.Mock(side_effect=RuntimeError("summary model unreachable at http://127.0.0.1:1"))
        with mock.patch("spitball.calendar.for_call", return_value=self._decision(["Priya Nair", "Alex Demo"])), \
             mock.patch("spitball.process.transcribe", return_value=_normalized_fixture("deepgram_multi_speaker.json", self.cfg)), \
             mock.patch("spitball.speakers._chat", chat), \
             mock.patch("spitball.process.summarize", return_value="# T\n\n## Summary\n- x"):
            result = process.process(self.call_dir, self.meta, self.cfg)
        text = (Path(result["dir"]) / "transcript.md").read_text()
        self.assertIn("**[00:00:00] Speaker 1:**", text)
        self.assertIn("**Note:** speaker names unavailable: summary model unreachable", text)

    def test_mismatch_between_invite_and_voices_is_noted(self):
        reply = json.dumps({"Speaker 1": None, "Speaker 2": None})
        result, _, _, _ = self._run(["Priya Nair", "Alex Demo", "Sam Third"], chat_reply=reply)
        text = (Path(result["dir"]) / "transcript.md").read_text()
        self.assertIn("**Note:** the invite lists 3 other people; 2 voices were found on the far side.", text)
        self.assertNotIn("**Speakers:**", text)  # nothing named: the bare labels say all there is

    def test_rename_by_hand_rerenders_outputs_and_survives_reprocess(self):
        export = self.tmp_path / "Notes"
        cfg = dict(self.cfg, export_dir=str(export))
        reply = json.dumps({"Speaker 1": {"name": "Priya Nair", "confidence": "high", "evidence": "e"}})
        result, _, _, _ = self._run(["Priya Nair", "Alex Demo"], chat_reply=reply, cfg=cfg)
        final_dir = Path(result["dir"])
        # A wrong automatic name and an unnamed second voice, fixed by hand.
        with mock.patch("spitball.process.summarize", side_effect=AssertionError("no re-summarize")), \
             mock.patch("spitball.speakers._chat", side_effect=AssertionError("no model")):
            rows = process.rename_speaker(final_dir, 2, "Alex Demo", cfg)
            process.rename_speaker(final_dir, 1, "Priya N.", cfg)
        self.assertEqual(rows[1]["label"], "Alex Demo")
        transcript = (final_dir / "transcript.md").read_text()
        self.assertIn("**[00:00:05] Alex Demo:** I disagree", transcript)
        self.assertIn("**[00:00:00] Priya N.:** I think", transcript)
        self.assertIn("**Speakers:** Speaker 1 = Priya N., Speaker 2 = Alex Demo", transcript)
        summary = (final_dir / "summary.md").read_text()
        self.assertIn("- Alex Demo will wait.", summary)          # the summary's own wording was rewritten
        self.assertIn("**Speakers:** Speaker 1 = Priya N.", summary)
        exported = (export / f"{final_dir.name}.md").read_text()
        self.assertIn("**[00:00:05] Alex Demo:** I disagree", exported)
        self.assertIn("- Alex Demo will wait.", exported)
        # A reprocess: the model answers something else; the user's names win.
        reply2 = json.dumps({"Speaker 1": {"name": "Alex Demo", "confidence": "high", "evidence": "e"},
                             "Speaker 2": {"name": "Priya Nair", "confidence": "high", "evidence": "e"}})
        result2, _, _, _ = self._run(["Priya Nair", "Alex Demo"], chat_reply=reply2, cfg=cfg, call_dir=final_dir)
        transcript = (Path(result2["dir"]) / "transcript.md").read_text()
        self.assertIn("**[00:00:00] Priya N.:**", transcript)
        self.assertIn("**[00:00:05] Alex Demo:**", transcript)
        cache = json.loads((Path(result2["dir"]) / ".transcript.json").read_text())
        self.assertEqual({e["source"] for e in cache["speakers"].values()}, {"user"})

    def test_clear_goes_back_to_automatic(self):
        result, _, _, _ = self._run(None)
        final_dir = Path(result["dir"])
        process.rename_speaker(final_dir, 1, "Someone", self.cfg)
        self.assertIn("Someone:**", (final_dir / "transcript.md").read_text())
        rows = process.rename_speaker(final_dir, 1, "", self.cfg)
        self.assertEqual(rows[0]["label"], "Speaker 1")
        self.assertEqual(rows[0]["source"], "")
        self.assertIn("**[00:00:00] Speaker 1:**", (final_dir / "transcript.md").read_text())

    def test_list_and_bad_number(self):
        result, _, _, _ = self._run(None)
        rows = process.list_speakers(Path(result["dir"]), self.cfg)
        self.assertEqual([(r["n"], r["label"], r["id"]) for r in rows], [(1, "Speaker 1", 2), (2, "Speaker 2", 5)])
        with self.assertRaises(ValueError):
            process.rename_speaker(Path(result["dir"]), 3, "X", self.cfg)
        with self.assertRaises(RuntimeError):
            process.list_speakers(self.tmp_path / "nowhere", self.cfg)

    def test_reused_live_transcript_is_offered_to_the_split(self):
        live = {"provider": "local", "model": "m", "note": "live transcript", "utterances": [
            {"channel": 0, "speaker": 0, "start": 0.0, "end": 3.0, "transcript": "hey"},
            {"channel": 1, "speaker": 0, "start": 4.0, "end": 9.0, "transcript": "of course glad to help " * 3}]}
        (self.call_dir / ".live.json").write_text(json.dumps(live))
        cfg = dict(self.cfg, transcription_provider="local")

        def fake_split(audio, normalized, cfg_, expected, transcribe_piece=None):
            normalized["diarization"] = {"ran": False, "engine": "sherpa-onnx", "expected": expected, "reason": "test"}
            return normalized

        with mock.patch("spitball.calendar.for_call", return_value=self._decision(["A One", "B Two", "C Three"])), \
             mock.patch("spitball.process.transcribe") as t, \
             mock.patch("spitball.diarize.split_transcript", side_effect=fake_split) as split, \
             mock.patch("spitball.speakers._chat", return_value="{}"), \
             mock.patch("spitball.process.summarize", return_value="# T\n\n## Summary\n- x"):
            result = process.process(self.call_dir, self.meta, cfg)
        t.assert_not_called()
        split.assert_called_once()
        self.assertEqual(split.call_args[0][3], 3)
        self.assertIsNotNone(split.call_args[1]["transcribe_piece"])
        cache = json.loads((Path(result["dir"]) / ".transcript.json").read_text())
        self.assertEqual(cache["diarization"]["reason"], "test")

    def test_cached_transcript_is_not_split_again(self):
        result, _, _, _ = self._run(None)
        with mock.patch("spitball.diarize.split_transcript", side_effect=AssertionError("cached: no split")), \
             mock.patch("spitball.calendar.for_call", return_value=None), \
             mock.patch("spitball.process.summarize", return_value="# T\n\n## Summary\n- x"):
            process.process(Path(result["dir"]), {}, self.cfg)

    def test_tiny_far_voice_folds_into_its_neighbor(self):
        normalized = {"provider": "deepgram", "model": "nova-3", "utterances": [
            {"channel": 1, "speaker": 0, "start": 0.0, "end": 12.0, "transcript": "a long first stretch of talk " * 3},
            {"channel": 1, "speaker": 3, "start": 12.5, "end": 13.2, "transcript": "mm-hm"},
            {"channel": 1, "speaker": 1, "start": 14.0, "end": 30.0, "transcript": "the second person at length " * 3},
            {"channel": 0, "speaker": 0, "start": 31.0, "end": 32.0, "transcript": "thanks"}]}
        result, _, _, _ = self._run(None, transcribe_return=normalized)
        text = (Path(result["dir"]) / "transcript.md").read_text()
        self.assertNotIn("Speaker 3", text)
        self.assertIn("a long first stretch of talk a long first stretch of talk a long first stretch of talk mm-hm", text)
        cache = json.loads((Path(result["dir"]) / ".transcript.json").read_text())
        self.assertEqual(sorted(cache["speakers"]), ["1", "2"])
        self.assertEqual(cache["utterances"][1]["speaker"], 3)  # the provider's ids are never rewritten
