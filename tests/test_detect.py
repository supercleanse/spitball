import json
import subprocess
import unittest
from unittest import mock

from spitball import detect

CALL_APPS_CFG = {
    "zoom": "Zoom", "chromium": "Chromium", "chrome": "Chrome", "brave": "Brave",
    "firefox": "Firefox", "slack": "Slack", "teams": "Teams", "discord": "Discord",
    "signal": "Signal", "webex": "Webex", "whatsapp": "WhatsApp",
}


def _run(source_outputs, sources_short="[]"):
    """Build a fake subprocess.run that answers pactl's two calls."""
    def fake_run(cmd, capture_output=True, text=True, timeout=5):
        result = mock.Mock()
        if "source-outputs" in cmd:
            result.stdout = source_outputs
        elif "sources" in cmd and "short" in cmd:
            result.stdout = sources_short
        else:
            result.stdout = "[]"
        return result
    return fake_run


def stream(app_name="", binary="", media="source", source=1):
    return {
        "index": 1,
        "source": source,
        "properties": {
            "application.name": app_name,
            "application.process.binary": binary,
            "media.name": media,
        },
    }


class TestCallApps(unittest.TestCase):
    def test_matched_by_binary(self):
        streams = [stream(binary="zoom", media="Zoom Meeting")]
        with mock.patch("subprocess.run", side_effect=_run(json.dumps(streams))):
            self.assertEqual(detect.call_apps(CALL_APPS_CFG), {"Zoom"})

    def test_matched_by_name(self):
        streams = [stream(app_name="Google Chrome", media="Chrome input")]
        with mock.patch("subprocess.run", side_effect=_run(json.dumps(streams))):
            self.assertEqual(detect.call_apps(CALL_APPS_CFG), {"Chrome"})

    def test_multiple_known_apps(self):
        streams = [
            stream(binary="zoom", media="m1", source=1),
            stream(app_name="Slack", media="m2", source=2),
        ]
        sources = [{"index": 1, "name": "alsa_input.mic"}, {"index": 2, "name": "alsa_input.mic2"}]
        with mock.patch("subprocess.run", side_effect=_run(json.dumps(streams), json.dumps(sources))):
            self.assertEqual(detect.call_apps(CALL_APPS_CFG), {"Zoom", "Slack"})

    def test_own_stream_ignored(self):
        streams = [stream(app_name="spitball", binary="ffmpeg", media="spitball")]
        with mock.patch("subprocess.run", side_effect=_run(json.dumps(streams))):
            self.assertEqual(detect.call_apps(CALL_APPS_CFG), set())

    def test_monitor_source_ignored(self):
        streams = [stream(binary="zoom", media="Zoom Meeting", source=7)]
        sources = [{"index": 7, "name": "alsa_output.pci-0000_00_1f.3.analog-stereo.monitor"}]
        with mock.patch("subprocess.run", side_effect=_run(json.dumps(streams), json.dumps(sources))):
            self.assertEqual(detect.call_apps(CALL_APPS_CFG), set())

    def test_peak_detect_ignored(self):
        streams = [stream(binary="zoom", media="Peak detect")]
        with mock.patch("subprocess.run", side_effect=_run(json.dumps(streams))):
            self.assertEqual(detect.call_apps(CALL_APPS_CFG), set())

    def test_peak_detect_ignored_case_insensitive(self):
        streams = [stream(binary="zoom", media="PEAK DETECT")]
        with mock.patch("subprocess.run", side_effect=_run(json.dumps(streams))):
            self.assertEqual(detect.call_apps(CALL_APPS_CFG), set())

    def test_unknown_app_ignored(self):
        streams = [stream(binary="mystery-app", app_name="Mystery", media="mic")]
        with mock.patch("subprocess.run", side_effect=_run(json.dumps(streams))):
            self.assertEqual(detect.call_apps(CALL_APPS_CFG), set())

    def test_pactl_failure_returns_empty(self):
        with mock.patch("subprocess.run", side_effect=OSError("pactl not found")):
            self.assertEqual(detect.call_apps(CALL_APPS_CFG), set())

    def test_pactl_bad_json_returns_empty(self):
        with mock.patch("subprocess.run", side_effect=_run("not json")):
            self.assertEqual(detect.call_apps(CALL_APPS_CFG), set())

    def test_pactl_timeout_returns_empty(self):
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="pactl", timeout=5)):
            self.assertEqual(detect.call_apps(CALL_APPS_CFG), set())

    def test_no_streams_skips_source_lookup(self):
        # _source_names() is only called when there ARE streams -- an empty
        # source-outputs list must not even shell out a second time.
        calls = []

        def fake_run(cmd, capture_output=True, text=True, timeout=5):
            calls.append(cmd)
            result = mock.Mock()
            result.stdout = "[]"
            return result

        with mock.patch("subprocess.run", side_effect=fake_run):
            self.assertEqual(detect.call_apps(CALL_APPS_CFG), set())
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
