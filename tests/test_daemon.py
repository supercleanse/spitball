"""Daemon state-machine tests. Every test runs inside isolated_runtime() (its
own temp RUNTIME_DIR/STATE_DIR/CONFIG_FILE) and mocks detect.call_apps(),
Recording, notify, and (where timing matters) time.time() -- never touches
the live daemon's socket, state file, ~/Calls, or a real mic/ffmpeg."""
import json
import signal
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from spitball.detect import OWN_APP_NAME
from tests.testutil import (FakeClock, Gate, isolated_runtime, make_fake_recording,
                             new_daemon, track_threads)

STATE_KEYS = {"state", "app", "started_at", "auto_record", "message", "last_call", "updated_at"}


class DaemonTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp = Path(self.tmpdir.name)
        self._rt_cm = isolated_runtime(self.tmp)
        self.config = self._rt_cm.__enter__()
        self.addCleanup(self._rt_cm.__exit__, None, None, None)
        self._notify_patch = mock.patch("spitball.daemon.notify")
        self.notify = self._notify_patch.start()
        self.addCleanup(self._notify_patch.stop)

    def assertValidState(self, state: dict):
        self.assertEqual(set(state.keys()), STATE_KEYS)
        self.assertIn(state["state"], ("offline", "idle", "detected", "recording", "processing", "error"))

    def read_state_file(self):
        return json.loads(self.config.STATE_FILE.read_text())


class TestSnapshotShape(DaemonTestCase):
    def test_snapshot_matches_contract_fields(self):
        d = new_daemon(self.tmp)
        self.assertValidState(d.snapshot())

    def test_publish_writes_atomically_and_matches_contract(self):
        d = new_daemon(self.tmp)
        d.publish()
        self.assertValidState(self.read_state_file())
        # No leftover temp file from the atomic-write dance.
        leftovers = [p for p in self.config.RUNTIME_DIR.iterdir() if p.name != "state.json"
                     and p.name != "ctl.sock"]
        self.assertEqual(leftovers, [])


class TestDetectionTiming(DaemonTestCase):
    def test_detect_after_s_delay(self):
        clock = FakeClock()
        d = new_daemon(self.tmp, detect_after_s=5, end_after_s=5)
        with mock.patch("spitball.daemon.time.time", side_effect=clock), \
             mock.patch("spitball.daemon.detect.call_apps", return_value={"Zoom"}):
            d.tick()  # first sighting -- not detected yet
            self.assertEqual(d.state, "idle")
            clock.advance(4)
            d.tick()  # still under detect_after_s
            self.assertEqual(d.state, "idle")
            clock.advance(1)
            d.tick()  # now >= detect_after_s since first sighting
            self.assertEqual(d.state, "detected")
            self.assertEqual(d.app, "Zoom")

    def test_end_after_s_rides_out_brief_gap(self):
        clock = FakeClock()
        d = new_daemon(self.tmp, detect_after_s=0, end_after_s=5)
        with mock.patch("spitball.daemon.time.time", side_effect=clock):
            with mock.patch("spitball.daemon.detect.call_apps", return_value={"Zoom"}):
                d.tick()
            self.assertEqual(d.state, "detected")
            clock.advance(2)  # a brief gap, under end_after_s
            with mock.patch("spitball.daemon.detect.call_apps", return_value=set()):
                d.tick()
            self.assertEqual(d.state, "detected")  # still present, rode out the gap

    def test_app_gone_past_end_after_s_returns_to_idle(self):
        clock = FakeClock()
        d = new_daemon(self.tmp, detect_after_s=0, end_after_s=5)
        with mock.patch("spitball.daemon.time.time", side_effect=clock):
            with mock.patch("spitball.daemon.detect.call_apps", return_value={"Zoom"}):
                d.tick()
            self.assertEqual(d.state, "detected")
            clock.advance(6)
            with mock.patch("spitball.daemon.detect.call_apps", return_value=set()):
                d.tick()
            self.assertEqual(d.state, "idle")


class TestDetectedStartAutoStop(DaemonTestCase):
    def test_detected_then_manual_start_then_auto_stop_when_app_leaves(self):
        d = new_daemon(self.tmp, detect_after_s=0, end_after_s=2)
        with mock.patch("spitball.daemon.detect.call_apps", return_value={"Zoom"}):
            d.tick()
        self.assertEqual(d.state, "detected")

        rec = make_fake_recording(alive=True, stop_duration=999)  # long enough to keep
        with mock.patch("spitball.daemon.Recording", return_value=rec):
            result = d.handle("start")
        self.assertTrue(result["ok"])
        self.assertEqual(d.state, "recording")
        self.assertEqual(d.rec_origin, "detected")

        # The app releases the mic; once "present" is empty the live recording
        # auto-stops (origin == "detected").
        with track_threads() as threads, \
             mock.patch("spitball.process.process",
                        return_value={"dir": str(d.rec_dir), "title": "T",
                                       "summary": str(d.rec_dir / "summary.md"), "ended_at": 1}):
            with mock.patch("spitball.daemon.detect.call_apps", return_value=set()), \
                 mock.patch("time.time", return_value=rec.started_at + 999):
                d.tick()
            for t in threads:
                t.join(timeout=5)
        self.assertIsNone(d.rec)
        self.assertIn("Call ended", [c.args[0] for c in self.notify.call_args_list])

    def test_manual_start_does_not_auto_stop_when_app_absent_throughout(self):
        d = new_daemon(self.tmp, detect_after_s=0, end_after_s=2)
        rec = make_fake_recording(alive=True, stop_duration=999)
        with mock.patch("spitball.daemon.Recording", return_value=rec):
            d.handle("start")
        self.assertEqual(d.rec_origin, "manual")
        with mock.patch("spitball.daemon.detect.call_apps", return_value=set()):
            d.tick()
            d.tick()
        self.assertIsNotNone(d.rec)  # still recording -- manual origin never auto-stops
        self.assertEqual(d.state, "recording")


class TestManualStopSuppressesReprompt(DaemonTestCase):
    def test_manual_stop_of_detected_call_suppresses_immediate_reprompt(self):
        d = new_daemon(self.tmp, detect_after_s=0, end_after_s=5, min_manual_s=1)
        with mock.patch("spitball.daemon.detect.call_apps", return_value={"Zoom"}):
            d.tick()
        rec = make_fake_recording(alive=True, stop_duration=999)
        with mock.patch("spitball.daemon.Recording", return_value=rec):
            d.handle("start")
        with track_threads() as threads, \
             mock.patch("spitball.process.transcribe", return_value={"results": {"utterances": []}}), \
             mock.patch("spitball.process.summarize", return_value="# T\n\nbody"):
            d.handle("stop")
            for t in threads:
                t.join(timeout=5)
        # App is still present, but we just dismissed it by stopping manually.
        with mock.patch("spitball.daemon.detect.call_apps", return_value={"Zoom"}):
            d.tick()
        self.assertNotEqual(d.state, "detected")


class TestDismiss(DaemonTestCase):
    def test_dismiss_returns_to_idle(self):
        d = new_daemon(self.tmp, detect_after_s=0, end_after_s=5)
        with mock.patch("spitball.daemon.detect.call_apps", return_value={"Zoom"}):
            d.tick()
        self.assertEqual(d.state, "detected")
        d.handle("dismiss")
        self.assertEqual(d.state, "idle")
        # Still present but dismissed: stays idle on the next tick.
        with mock.patch("spitball.daemon.detect.call_apps", return_value={"Zoom"}):
            d.tick()
        self.assertEqual(d.state, "idle")

    def test_dismiss_clears_error(self):
        d = new_daemon(self.tmp)
        d.error = "Recorder stopped unexpectedly"
        d._idle_state()
        self.assertEqual(d.state, "error")
        d.handle("dismiss")
        self.assertEqual(d.state, "idle")
        self.assertEqual(d.error, "")


class TestAutoRecordPersistence(DaemonTestCase):
    def test_auto_on_off_toggle_and_persisted(self):
        d = new_daemon(self.tmp)
        self.assertFalse(d.auto_record)
        r = d.handle("auto", "on")
        self.assertTrue(r["auto_record"])
        self.assertTrue(d.auto_record)

        persisted = json.loads(self.config.PERSIST_FILE.read_text())
        self.assertTrue(persisted["auto_record"])

        d2 = new_daemon(self.tmp)  # fresh instance re-reads persist.json
        self.assertTrue(d2.auto_record)

        d2.handle("auto", "off")
        self.assertFalse(d2.auto_record)
        d2.handle("auto", "toggle")
        self.assertTrue(d2.auto_record)
        d2.handle("auto", "toggle")
        self.assertFalse(d2.auto_record)

    def test_last_call_persisted_across_instances(self):
        d = new_daemon(self.tmp)
        d.last_call = {"dir": "/x", "title": "T", "ended_at": 1, "summary": "/x/summary.md"}
        d._save_persist()
        d2 = new_daemon(self.tmp)
        self.assertEqual(d2.last_call["title"], "T")


class TestAutoRecordOnDetect(DaemonTestCase):
    def test_auto_record_starts_recording_without_a_command(self):
        d = new_daemon(self.tmp, detect_after_s=0, end_after_s=5)
        d.auto_record = True
        rec = make_fake_recording(alive=True)
        with mock.patch("spitball.daemon.Recording", return_value=rec), \
             mock.patch("spitball.daemon.detect.call_apps", return_value={"Zoom"}):
            d.tick()
        self.assertEqual(d.state, "recording")
        self.assertEqual(d.rec_origin, "detected")
        messages = [c.args for c in self.notify.call_args_list]
        self.assertTrue(any("Auto-record is on." in a for a in messages))


class TestShortRecordingDiscarded(DaemonTestCase):
    def test_short_manual_recording_is_discarded(self):
        d = new_daemon(self.tmp, min_manual_s=10)
        rec = make_fake_recording(alive=True, stop_duration=2.0)
        with mock.patch("spitball.daemon.Recording", return_value=rec):
            d.handle("start")
        call_dir = d.rec_dir
        self.assertTrue(call_dir.exists())
        r = d.handle("stop")
        self.assertIn("discarded", r.get("note", ""))
        self.assertFalse(call_dir.exists())
        self.assertEqual(d.processing, 0)


class TestRecorderDeathAndDismiss(DaemonTestCase):
    def test_recorder_death_sets_error_then_dismiss_clears_it(self):
        d = new_daemon(self.tmp, min_manual_s=10)
        rec = make_fake_recording(alive=True, stop_duration=1.0)  # too short once stopped
        with mock.patch("spitball.daemon.Recording", return_value=rec):
            d.handle("start")
        rec.alive.return_value = False  # simulate the recorder dying
        with mock.patch("spitball.daemon.detect.call_apps", return_value=set()):
            d.tick()
        self.assertEqual(d.state, "error")
        self.assertIn("Recorder stopped unexpectedly", d.error)
        self.assertIsNone(d.rec)

        d.handle("dismiss")
        self.assertEqual(d.state, "idle")
        self.assertEqual(d.error, "")


class TestFfmpegStartFailure(DaemonTestCase):
    def test_start_failure_sets_error_and_discards_dir(self):
        d = new_daemon(self.tmp)
        rec = make_fake_recording(alive=False)
        with mock.patch("spitball.daemon.Recording", return_value=rec):
            r = d.handle("start")
        self.assertFalse(r["ok"])
        self.assertIn("Recorder failed to start", r["error"])
        self.assertEqual(d.state, "error")
        self.assertIsNone(d.rec)
        # critical notification fired
        urgencies = [c.args[2] if len(c.args) > 2 else c.kwargs.get("urgency") for c in self.notify.call_args_list]
        self.assertIn("critical", urgencies)


class TestProcessingSuccessFailure(DaemonTestCase):
    def test_processing_success_updates_last_call(self):
        d = new_daemon(self.tmp, min_manual_s=1)
        rec = make_fake_recording(alive=True, stop_duration=100.0)
        with mock.patch("spitball.daemon.Recording", return_value=rec):
            d.handle("start")
        result = {"dir": str(d.rec_dir), "title": "A Title", "summary": str(d.rec_dir / "summary.md"),
                  "ended_at": 123}
        with track_threads() as threads, mock.patch("spitball.process.process", return_value=result):
            d.handle("stop")
            for t in threads:
                t.join(timeout=5)
        self.assertEqual(d.last_call["title"], "A Title")
        persisted = json.loads(self.config.PERSIST_FILE.read_text())
        self.assertEqual(persisted["last_call"]["title"], "A Title")

    def test_processing_failure_sets_error(self):
        d = new_daemon(self.tmp, min_manual_s=1)
        rec = make_fake_recording(alive=True, stop_duration=100.0)
        with mock.patch("spitball.daemon.Recording", return_value=rec):
            d.handle("start")
        call_dir_name = d.rec_dir.name
        with track_threads() as threads, \
             mock.patch("spitball.process.process", side_effect=RuntimeError("boom")):
            d.handle("stop")
            for t in threads:
                t.join(timeout=5)
        self.assertIn("boom", d.error)
        self.assertIn(call_dir_name, d.error)
        self.assertEqual(d.processing, 0)


class TestLiveCallWinsOverProcessing(DaemonTestCase):
    def test_new_detection_overrides_processing_display_while_it_continues(self):
        d = new_daemon(self.tmp, detect_after_s=0, end_after_s=5, min_manual_s=1)
        rec = make_fake_recording(alive=True, stop_duration=100.0)
        with mock.patch("spitball.daemon.Recording", return_value=rec):
            d.handle("start")

        gate = Gate()
        fake_result = {"dir": str(d.rec_dir), "title": "T", "summary": str(d.rec_dir / "summary.md"),
                        "ended_at": 1}

        with track_threads() as threads, \
             mock.patch("spitball.process.process", side_effect=gate.wait_then(lambda *a, **kw: fake_result)):
            d.handle("stop")
            self.assertTrue(gate.entered.wait(5))
            self.assertEqual(d.processing, 1)

            # A brand-new call is detected while the previous one is still processing.
            with mock.patch("spitball.daemon.detect.call_apps", return_value={"Slack"}):
                d.tick()
            self.assertEqual(d.state, "detected")
            self.assertEqual(d.app, "Slack")
            state_on_disk = json.loads(self.config.STATE_FILE.read_text())
            self.assertEqual(state_on_disk["state"], "detected")

            gate.release.set()
            for t in threads:
                t.join(timeout=5)

        self.assertEqual(d.last_call["title"], "T")


class TestControlHandlers(DaemonTestCase):
    def test_status_reports_present(self):
        d = new_daemon(self.tmp, detect_after_s=0, end_after_s=5)
        with mock.patch("spitball.daemon.detect.call_apps", return_value={"Zoom"}):
            d.tick()
        r = d.handle("status")
        self.assertEqual(r["present"], ["Zoom"])

    def test_toggle_starts_and_stops(self):
        d = new_daemon(self.tmp, min_manual_s=1)
        rec = make_fake_recording(alive=True, stop_duration=999)
        with mock.patch("spitball.daemon.Recording", return_value=rec):
            r1 = d.handle("toggle")
        self.assertEqual(d.state, "recording")
        with track_threads() as threads, \
             mock.patch("spitball.process.transcribe", return_value={"results": {"utterances": []}}), \
             mock.patch("spitball.process.summarize", return_value="# T\n\nbody"):
            r2 = d.handle("toggle")
            for t in threads:
                t.join(timeout=5)
        self.assertTrue(r1["ok"])
        self.assertTrue(r2["ok"])
        self.assertIsNone(d.rec)

    def test_reload_reloads_config(self):
        d = new_daemon(self.tmp)
        self.config.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.config.CONFIG_FILE.write_text(json.dumps({"my_name": "Morgan"}))
        d.handle("reload")
        self.assertEqual(d.cfg["my_name"], "Morgan")

    def test_unknown_command(self):
        d = new_daemon(self.tmp)
        r = d.handle("not-a-real-command")
        self.assertFalse(r["ok"])


class TestShutdownWritesOffline(DaemonTestCase):
    def test_run_writes_offline_state_on_exit(self):
        d = new_daemon(self.tmp)
        d.serve = lambda: None  # never actually bind a socket
        with mock.patch("spitball.daemon.detect.Watcher") as watcher_cls:
            watcher_cls.return_value.start = mock.Mock()
            d.tick = mock.Mock(side_effect=SystemExit(0))
            with self.assertRaises(SystemExit):
                d.run()
        state = json.loads(self.config.STATE_FILE.read_text())
        self.assertEqual(state, {"state": "offline"})

    def test_run_stops_a_live_recording_before_going_offline(self):
        d = new_daemon(self.tmp, min_manual_s=1)
        rec = make_fake_recording(alive=True, stop_duration=999)
        with mock.patch("spitball.daemon.Recording", return_value=rec):
            d.handle("start")
        d.serve = lambda: None
        with mock.patch("spitball.daemon.detect.Watcher") as watcher_cls, \
             track_threads() as threads, \
             mock.patch("spitball.process.process",
                        return_value={"dir": str(d.rec_dir), "title": "T",
                                       "summary": str(d.rec_dir / "summary.md"), "ended_at": 1}):
            watcher_cls.return_value.start = mock.Mock()
            d.tick = mock.Mock(side_effect=SystemExit(0))
            with self.assertRaises(SystemExit):
                d.run()
            for t in threads:
                t.join(timeout=5)
        rec.stop.assert_called_once()
        state = json.loads(self.config.STATE_FILE.read_text())
        self.assertEqual(state, {"state": "offline"})


class TestRecover(DaemonTestCase):
    """recover() must never touch a real process: pgrep and os.kill are always
    mocked here, so a stray recorder from a previous run is only ever a
    fabricated pid the test made up -- never a real PID on this machine."""

    def _calls_dir(self, d):
        calls = Path(d.cfg["calls_dir"])
        calls.mkdir(parents=True, exist_ok=True)
        return calls

    def test_kills_stray_recorder_via_pgrep_and_sigint(self):
        d = new_daemon(self.tmp)
        pgrep_result = mock.Mock(stdout="4321\n")
        with mock.patch("spitball.daemon.subprocess.run", return_value=pgrep_result) as run_mock, \
             mock.patch("spitball.daemon.os.kill") as kill_mock, \
             mock.patch("spitball.daemon.time.sleep") as sleep_mock:
            d.recover()
        cmd = run_mock.call_args.args[0]
        self.assertEqual(cmd[0], "pgrep")
        self.assertIn("-f", cmd)
        self.assertIn(f"-name {OWN_APP_NAME} ", cmd[-1])
        kill_mock.assert_called_once_with(4321, signal.SIGINT)
        sleep_mock.assert_called_once_with(3)

    def test_no_stray_recorder_skips_kill_and_sleep(self):
        d = new_daemon(self.tmp)
        pgrep_result = mock.Mock(stdout="")
        with mock.patch("spitball.daemon.subprocess.run", return_value=pgrep_result), \
             mock.patch("spitball.daemon.os.kill") as kill_mock, \
             mock.patch("spitball.daemon.time.sleep") as sleep_mock:
            d.recover()
        kill_mock.assert_not_called()
        sleep_mock.assert_not_called()

    def test_pgrep_failure_is_swallowed(self):
        d = new_daemon(self.tmp)
        with mock.patch("spitball.daemon.subprocess.run", side_effect=OSError("no pgrep")), \
             mock.patch("spitball.daemon.os.kill") as kill_mock:
            d.recover()  # must not raise
        kill_mock.assert_not_called()

    def test_missing_calls_dir_returns_early(self):
        d = new_daemon(self.tmp)  # calls_dir was never created
        with mock.patch("spitball.daemon.subprocess.run", return_value=mock.Mock(stdout="")), \
             mock.patch("spitball.daemon.os.kill"):
            d.recover()  # no exception

    def test_folder_with_meta_json_is_processed(self):
        d = new_daemon(self.tmp)
        calls = self._calls_dir(d)
        call_dir = calls / "2026-09-28-1000-zoom"
        call_dir.mkdir()
        (call_dir / "audio.opus").write_bytes(b"x")
        meta = {"app": "Zoom", "started_at": 1000.0, "duration": 90.0}
        (call_dir / ".meta.json").write_text(json.dumps(meta))

        with mock.patch("spitball.daemon.subprocess.run", return_value=mock.Mock(stdout="")), \
             track_threads() as threads, \
             mock.patch("spitball.process.process",
                        return_value={"dir": str(call_dir), "title": "T",
                                       "summary": str(call_dir / "summary.md"), "ended_at": 1}) as proc_mock:
            d.recover()
            for t in threads:
                t.join(timeout=5)
        proc_mock.assert_called_once()
        called_meta = proc_mock.call_args.args[1]
        self.assertEqual(called_meta["app"], "Zoom")
        self.assertEqual(d.last_call["title"], "T")

    def test_folder_without_meta_json_rebuilds_from_ffprobe_and_processes(self):
        d = new_daemon(self.tmp, min_manual_s=10)
        calls = self._calls_dir(d)
        call_dir = calls / "2026-09-28-1000-manual"
        call_dir.mkdir()
        (call_dir / "audio.opus").write_bytes(b"x")

        with mock.patch("spitball.daemon.subprocess.run", return_value=mock.Mock(stdout="")), \
             mock.patch("spitball.process.audio_seconds", return_value=45.0), \
             track_threads() as threads, \
             mock.patch("spitball.process.process",
                        return_value={"dir": str(call_dir), "title": "T",
                                       "summary": str(call_dir / "summary.md"), "ended_at": 1}) as proc_mock:
            d.recover()
            for t in threads:
                t.join(timeout=5)
        proc_mock.assert_called_once()
        self.assertTrue((call_dir / ".meta.json").exists())
        rebuilt = json.loads((call_dir / ".meta.json").read_text())
        self.assertEqual(rebuilt["duration"], 45.0)

    def test_folder_without_meta_json_shorter_than_min_manual_s_is_discarded(self):
        d = new_daemon(self.tmp, min_manual_s=10)
        calls = self._calls_dir(d)
        call_dir = calls / "2026-09-28-1000-manual"
        call_dir.mkdir()
        (call_dir / "audio.opus").write_bytes(b"x")

        with mock.patch("spitball.daemon.subprocess.run", return_value=mock.Mock(stdout="")), \
             mock.patch("spitball.process.audio_seconds", return_value=3.0), \
             mock.patch("spitball.process.process") as proc_mock:
            d.recover()
        proc_mock.assert_not_called()
        self.assertFalse(call_dir.exists())

    def test_folder_with_summary_already_is_skipped(self):
        d = new_daemon(self.tmp)
        calls = self._calls_dir(d)
        call_dir = calls / "2026-09-28-1000-zoom"
        call_dir.mkdir()
        (call_dir / "audio.opus").write_bytes(b"x")
        (call_dir / "summary.md").write_text("# done")

        with mock.patch("spitball.daemon.subprocess.run", return_value=mock.Mock(stdout="")), \
             mock.patch("spitball.process.process") as proc_mock:
            d.recover()
        proc_mock.assert_not_called()

    def test_folder_without_audio_is_skipped(self):
        d = new_daemon(self.tmp)
        calls = self._calls_dir(d)
        call_dir = calls / "2026-09-28-1000-empty"
        call_dir.mkdir()

        with mock.patch("spitball.daemon.subprocess.run", return_value=mock.Mock(stdout="")), \
             mock.patch("spitball.process.process") as proc_mock:
            d.recover()
        proc_mock.assert_not_called()
        self.assertTrue(call_dir.exists())  # left alone, not discarded


if __name__ == "__main__":
    unittest.main()
