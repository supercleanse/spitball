"""spitball.providers.deepgram tests: request shape, normalization, language
param, ready()/check(). test_process.py already covers raw_transcribe()'s
HTTP behavior in detail (as process.transcribe, kept working via the
provider dispatch) -- this file focuses on what's new here: normalize(),
the language setting, and ready()/check().
"""
import io
import os
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from spitball.providers import deepgram
from tests.testutil import load_fixture, make_cfg


class TestNormalize(unittest.TestCase):
    def test_flattens_results_utterances(self):
        raw = load_fixture("deepgram_single_speaker.json")
        normalized = deepgram.normalize(raw, {"deepgram_model": "nova-3"})
        self.assertEqual(normalized["provider"], "deepgram")
        self.assertEqual(normalized["model"], "nova-3")
        self.assertEqual(len(normalized["utterances"]), 7)
        first = normalized["utterances"][0]
        self.assertEqual(set(first), {"channel", "speaker", "start", "end", "transcript"})

    def test_empty_utterances(self):
        raw = load_fixture("deepgram_no_speech.json")
        normalized = deepgram.normalize(raw, {})
        self.assertEqual(normalized["utterances"], [])

    def test_missing_results_key_is_empty(self):
        self.assertEqual(deepgram.normalize({}, {})["utterances"], [])

    def test_model_falls_back_to_nova3(self):
        normalized = deepgram.normalize({"results": {"utterances": []}}, {})
        self.assertEqual(normalized["model"], "nova-3")


class TestLanguageParam(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.audio = Path(self.tmp.name) / "audio.opus"
        self.audio.write_bytes(b"x")
        self._env_patch = mock.patch.dict(os.environ, {}, clear=False)
        self._env_patch.start()
        os.environ.pop("DEEPGRAM_API_KEY", None)
        self.addCleanup(self._env_patch.stop)

    def _capture_url(self, cfg):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            return io.BytesIO(b'{"results": {"utterances": []}}')

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            deepgram.raw_transcribe(self.audio, cfg)
        return captured["url"]

    def test_default_language_is_en(self):
        cfg = make_cfg(Path(self.tmp.name), deepgram_api_key="k")
        url = self._capture_url(cfg)
        self.assertIn("language=en", url)
        self.assertNotIn("detect_language", url)

    def test_explicit_language_code(self):
        cfg = make_cfg(Path(self.tmp.name), deepgram_api_key="k", language="fr")
        url = self._capture_url(cfg)
        self.assertIn("language=fr", url)

    def test_auto_uses_detect_language(self):
        cfg = make_cfg(Path(self.tmp.name), deepgram_api_key="k", language="auto")
        url = self._capture_url(cfg)
        self.assertIn("detect_language=true", url)
        self.assertNotIn("language=auto", url)
        self.assertNotIn("&language=", url)


class TestReady(unittest.TestCase):
    def setUp(self):
        self._env_patch = mock.patch.dict(os.environ, {}, clear=False)
        self._env_patch.start()
        os.environ.pop("DEEPGRAM_API_KEY", None)
        self.addCleanup(self._env_patch.stop)

    def test_no_key_anywhere(self):
        self.assertEqual(deepgram.ready({"deepgram_api_key": "", "deepgram_api_key_command": ""}),
                          "Add a Deepgram key")

    def test_key_in_config_is_ready(self):
        self.assertEqual(deepgram.ready({"deepgram_api_key": "k"}), "")

    def test_key_command_configured_is_ready_without_running_it(self):
        with mock.patch("spitball.config.subprocess.run") as run_mock:
            result = deepgram.ready({"deepgram_api_key": "", "deepgram_api_key_command": "echo x"})
        self.assertEqual(result, "")
        run_mock.assert_not_called()  # cheap check: never executes the command

    def test_env_var_is_ready(self):
        with mock.patch.dict(os.environ, {"DEEPGRAM_API_KEY": "k"}):
            self.assertEqual(deepgram.ready({}), "")


class TestCheck(unittest.TestCase):
    def test_no_key_fails_without_network(self):
        with mock.patch("urllib.request.urlopen") as urlopen_mock:
            result = deepgram.check({"deepgram_api_key": ""})
        self.assertFalse(result["ok"])
        urlopen_mock.assert_not_called()

    def test_ok_on_200(self):
        with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(b"{}")) as m:
            m.return_value.__enter__ = lambda s: s
            m.return_value.__exit__ = lambda *a: False
            result = deepgram.check({"deepgram_api_key": "k"})
        self.assertTrue(result["ok"])

    def test_http_error_surfaces_code(self):
        def raise_error(req, timeout=None):
            raise urllib.error.HTTPError("url", 401, "Unauthorized", None, io.BytesIO(b"bad"))
        with mock.patch("urllib.request.urlopen", side_effect=raise_error):
            result = deepgram.check({"deepgram_api_key": "k"})
        self.assertFalse(result["ok"])
        self.assertIn("401", result["message"])

    def test_connection_error(self):
        def raise_error(req, timeout=None):
            raise urllib.error.URLError("unreachable")
        with mock.patch("urllib.request.urlopen", side_effect=raise_error):
            result = deepgram.check({"deepgram_api_key": "k"})
        self.assertFalse(result["ok"])
        self.assertIn("unreachable", result["message"])


if __name__ == "__main__":
    unittest.main()
