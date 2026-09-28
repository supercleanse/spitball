import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spitball.recorder import Recording
from spitball.detect import OWN_APP_NAME


def fake_default(kind_map):
    def _run(cmd, capture_output=True, text=True, timeout=5):
        result = mock.Mock()
        kind = cmd[1].replace("get-default-", "")
        result.stdout = kind_map.get(kind, "") + "\n"
        return result
    return _run


class TestRecordingArgv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "call" / "audio.opus"

    def _make(self, source="alsa_input.mic0", sink="alsa_output.speakers0"):
        popen = mock.Mock()
        popen.poll.return_value = None
        with mock.patch("subprocess.run", side_effect=fake_default({"source": source, "sink": sink})), \
             mock.patch("subprocess.Popen", return_value=popen) as popen_ctor:
            rec = Recording(self.path, bitrate="24k")
        return rec, popen_ctor

    def test_argv_shape(self):
        rec, popen_ctor = self._make()
        cmd = popen_ctor.call_args.args[0]
        self.assertEqual(cmd[0], "ffmpeg")
        self.assertIn("-nostdin", cmd)
        # two pulse inputs: mic first, then the sink's .monitor
        i_positions = [idx for idx, tok in enumerate(cmd) if tok == "-i"]
        self.assertEqual(len(i_positions), 2)
        mic_arg = cmd[i_positions[0] + 1]
        monitor_arg = cmd[i_positions[1] + 1]
        self.assertEqual(mic_arg, "alsa_input.mic0")
        self.assertEqual(monitor_arg, "alsa_output.speakers0.monitor")
        # both pulse inputs are named "spitball" so detect.py never sees itself
        self.assertEqual(cmd.count(OWN_APP_NAME), 2)
        self.assertIn("amerge=inputs=2", " ".join(cmd))
        self.assertIn("libopus", cmd)
        self.assertIn("24k", cmd)
        self.assertEqual(cmd[-1], str(self.path))
        rec.log.close()

    def test_monitor_falls_back_when_no_default_sink(self):
        rec, popen_ctor = self._make(sink="")
        cmd = popen_ctor.call_args.args[0]
        i_positions = [idx for idx, tok in enumerate(cmd) if tok == "-i"]
        monitor_arg = cmd[i_positions[1] + 1]
        self.assertEqual(monitor_arg, "@DEFAULT_MONITOR@")
        rec.log.close()

    def test_mic_defaults_to_default_when_no_default_source(self):
        rec, popen_ctor = self._make(source="")
        cmd = popen_ctor.call_args.args[0]
        i_positions = [idx for idx, tok in enumerate(cmd) if tok == "-i"]
        mic_arg = cmd[i_positions[0] + 1]
        self.assertEqual(mic_arg, "default")
        rec.log.close()

    def test_creates_parent_dir_and_log(self):
        rec, _ = self._make()
        self.assertTrue(self.path.parent.is_dir())
        self.assertTrue((self.path.parent / "ffmpeg.log").exists())
        rec.log.close()

    def test_alive_reflects_poll(self):
        rec, popen_ctor = self._make()
        self.assertTrue(rec.alive())
        rec.proc.poll.return_value = 0
        self.assertFalse(rec.alive())
        rec.log.close()


class TestRecordingStop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "audio.opus"

    def _make(self):
        popen = mock.Mock()
        popen.poll.return_value = None
        with mock.patch("subprocess.run", side_effect=fake_default({"source": "mic", "sink": "sink"})), \
             mock.patch("subprocess.Popen", return_value=popen):
            rec = Recording(self.path, bitrate="32k")
        return rec

    def test_stop_sends_sigint_and_waits(self):
        rec = self._make()
        rec.proc.wait.return_value = 0
        with mock.patch("time.time", return_value=rec.started_at + 5.0):
            duration = rec.stop()
        rec.proc.send_signal.assert_called_once_with(signal.SIGINT)
        rec.proc.wait.assert_called_once_with(timeout=10)
        rec.proc.kill.assert_not_called()
        self.assertAlmostEqual(duration, 5.0, places=3)

    def test_stop_kills_on_timeout(self):
        rec = self._make()
        rec.proc.wait.side_effect = [subprocess.TimeoutExpired(cmd="ffmpeg", timeout=10), 0]
        rec.stop()
        rec.proc.kill.assert_called_once()
        self.assertEqual(rec.proc.wait.call_count, 2)

    def test_stop_when_already_dead_does_not_signal(self):
        rec = self._make()
        rec.proc.poll.return_value = -9  # already dead
        rec.stop()
        rec.proc.send_signal.assert_not_called()

    def test_empty_ffmpeg_log_removed(self):
        rec = self._make()
        rec.proc.wait.return_value = 0
        log_path = self.path.with_name("ffmpeg.log")
        self.assertTrue(log_path.exists())
        rec.stop()
        self.assertFalse(log_path.exists())

    def test_nonempty_ffmpeg_log_kept(self):
        rec = self._make()
        rec.log.write("some ffmpeg error output\n")
        rec.log.flush()
        rec.proc.wait.return_value = 0
        rec.stop()
        log_path = self.path.with_name("ffmpeg.log")
        self.assertTrue(log_path.exists())
        self.assertIn("ffmpeg error", log_path.read_text())


if __name__ == "__main__":
    unittest.main()
