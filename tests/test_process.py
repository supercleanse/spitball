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
from tests.testutil import load_fixture, make_cfg


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
        self.assertEqual(process._slug("Weekly Sync with Curt"), "weekly-sync-with-curt")

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
        dg = load_fixture("deepgram_single_speaker.json")
        lines = process.build_transcript(dg, self.cfg)
        speakers_texts = [(round(s, 1), w, t) for s, w, t in lines]
        self.assertEqual(speakers_texts, [
            (0.0, "Them", "Hey how's it going today"),
            (2.5, "Morgan", "I'm doing great thanks and glad to be here"),
            (20.0, "Them", "Quick question for you about the deadline"),
            (40.0, "Morgan", "Sure, let's talk about it"),
        ])

    def test_echo_utterance_is_dropped(self):
        dg = load_fixture("deepgram_single_speaker.json")
        lines = process.build_transcript(dg, self.cfg)
        full_text = " ".join(t for _, _, t in lines)
        self.assertNotIn("hey how's it going today and", full_text)  # the echoed dup
        self.assertEqual(sum(1 for _, w, t in lines if "Hey how's it going today" in t), 1)

    def test_real_mic_speech_kept_when_not_an_echo(self):
        dg = {"results": {"utterances": [
            {"channel": 1, "start": 0.0, "end": 1.5, "speaker": 0,
             "transcript": "so what did you think of the proposal"},
            {"channel": 0, "start": 6.0, "end": 8.0, "speaker": 0,
             "transcript": "honestly I think we should rewrite the whole thing"},
        ]}}
        lines = process.build_transcript(dg, self.cfg)
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[1][1], "Morgan")
        self.assertIn("rewrite the whole thing", lines[1][2])

    def test_multi_far_speakers_labeled(self):
        dg = load_fixture("deepgram_multi_speaker.json")
        lines = process.build_transcript(dg, self.cfg)
        labels = [w for _, w, _ in lines]
        self.assertEqual(labels, ["Speaker 1", "Speaker 2", "Morgan"])

    def test_no_speech_returns_empty(self):
        dg = load_fixture("deepgram_no_speech.json")
        lines = process.build_transcript(dg, self.cfg)
        self.assertEqual(lines, [])

    def test_blank_transcript_utterances_filtered(self):
        dg = {"results": {"utterances": [
            {"channel": 0, "start": 0.0, "end": 1.0, "speaker": 0, "transcript": "   "},
            {"channel": 0, "start": 2.0, "end": 3.0, "speaker": 0, "transcript": "real words"},
        ]}}
        lines = process.build_transcript(dg, self.cfg)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0][2], "real words")

    def test_consecutive_same_speaker_merged_within_4s(self):
        dg = {"results": {"utterances": [
            {"channel": 1, "start": 0.0, "end": 1.0, "speaker": 0, "transcript": "part one"},
            {"channel": 1, "start": 2.0, "end": 3.0, "speaker": 0, "transcript": "part two"},
        ]}}
        lines = process.build_transcript(dg, self.cfg)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0][2], "part one part two")

    def test_gap_over_4s_not_merged(self):
        dg = {"results": {"utterances": [
            {"channel": 1, "start": 0.0, "end": 1.0, "speaker": 0, "transcript": "part one"},
            {"channel": 1, "start": 10.0, "end": 11.0, "speaker": 0, "transcript": "part two"},
        ]}}
        lines = process.build_transcript(dg, self.cfg)
        self.assertEqual(len(lines), 2)


class TestTranscribe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.audio = Path(self.tmp.name) / "audio.opus"
        self.audio.write_bytes(b"fake-opus-bytes")
        self.cfg = make_cfg(Path(self.tmp.name), deepgram_api_key="dg-test-key-123")
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
        self.assertEqual(result, {"results": {"utterances": []}})

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
        cfg = make_cfg(Path(self.tmp.name))  # deepgram_api_key/_command both empty
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
        self.dg_fixture = load_fixture("deepgram_single_speaker.json")

    def _patched(self, summarize_return="# Weekly Sync With Curt\n\n## Summary\n- talked",
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
        self.assertEqual(result["title"], "Weekly Sync With Curt")

    def test_folder_renamed_with_slug_once(self):
        p_transcribe, p_summarize = self._patched()
        with p_transcribe, p_summarize:
            result = process.process(self.call_dir, self.meta, self.cfg)
        final_dir = Path(result["dir"])
        self.assertTrue(final_dir.name.endswith("-weekly-sync-with-curt"))
        self.assertFalse(self.call_dir.exists())

    def test_reprocess_does_not_double_rename(self):
        p_transcribe, p_summarize = self._patched()
        with p_transcribe, p_summarize:
            result = process.process(self.call_dir, self.meta, self.cfg)
        final_dir = Path(result["dir"])
        p_transcribe2, p_summarize2 = self._patched(
            summarize_return="# Weekly Sync With Curt\n\n## Summary\n- talked more")
        with p_transcribe2 as t2, p_summarize2:
            result2 = process.process(final_dir, {}, self.cfg)
        t2.assert_not_called()  # cached .deepgram.json reused
        self.assertEqual(result2["dir"], result["dir"])

    def test_reprocess_reuses_cached_deepgram_json(self):
        p_transcribe, p_summarize = self._patched()
        with p_transcribe as t1, p_summarize:
            process.process(self.call_dir, self.meta, self.cfg)
        self.assertEqual(t1.call_count, 1)
        final_dir = next(iter(Path(self.cfg["calls_dir"]).glob("*")))
        with mock.patch("spitball.process.transcribe") as t2, \
             mock.patch("spitball.process.summarize", return_value="# T2\n\nx"):
            process.process(final_dir, {}, self.cfg)
        t2.assert_not_called()

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
        self.assertIn("Weekly Sync With Curt", exported_text)
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
            transcribe_return=load_fixture("deepgram_no_speech.json"))
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


if __name__ == "__main__":
    unittest.main()
