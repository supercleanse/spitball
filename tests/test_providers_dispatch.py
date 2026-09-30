"""spitball.providers (the dispatcher) tests: routes to the right provider
module by transcription_provider, and setup_needed() -- the cheap, no-network
check the daemon runs at start and after every reload."""
import unittest
from unittest import mock

from spitball import providers


class TestDispatch(unittest.TestCase):
    def test_defaults_to_local(self):
        with mock.patch("spitball.providers.local.transcribe", return_value={"provider": "local"}) as m:
            result = providers.transcribe("/fake.opus", {})
        m.assert_called_once()
        self.assertEqual(result["provider"], "local")

    def test_routes_to_deepgram(self):
        with mock.patch("spitball.providers.deepgram.transcribe", return_value={"provider": "deepgram"}) as m:
            providers.transcribe("/fake.opus", {"transcription_provider": "deepgram"})
        m.assert_called_once()

    def test_unknown_provider_raises(self):
        with self.assertRaises(RuntimeError):
            providers.transcribe("/fake.opus", {"transcription_provider": "not-a-real-one"})

    def test_check_dispatches_by_explicit_provider_arg(self):
        with mock.patch("spitball.providers.deepgram.check", return_value={"ok": True}) as m:
            providers.check({"transcription_provider": "local"}, provider="deepgram")
        m.assert_called_once()


class TestSetupNeeded(unittest.TestCase):
    def test_empty_when_ready(self):
        with mock.patch("spitball.providers.local.ready", return_value=""):
            self.assertEqual(providers.setup_needed({"transcription_provider": "local"}), "")

    def test_returns_the_provider_reason(self):
        with mock.patch("spitball.providers.local.ready", return_value="Install dictation (voxtype)"):
            self.assertEqual(providers.setup_needed({"transcription_provider": "local"}),
                              "Install dictation (voxtype)")

    def test_deepgram_reason(self):
        with mock.patch("spitball.providers.deepgram.ready", return_value="Add a Deepgram key"):
            reason = providers.setup_needed({"transcription_provider": "deepgram"})
        self.assertEqual(reason, "Add a Deepgram key")

    def test_unknown_provider_is_a_reason_not_a_crash(self):
        reason = providers.setup_needed({"transcription_provider": "nope"})
        self.assertIn("nope", reason)


if __name__ == "__main__":
    unittest.main()
