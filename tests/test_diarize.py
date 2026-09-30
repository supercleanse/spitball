"""spitball.diarize tests. The real sherpa-onnx worker never runs here
unless SPITBALL_DIARIZE_TEST_DIR points at a directory with a venv that has
sherpa-onnx and the two models in place (see TestRealDiarization); the
protocol is otherwise exercised against a fake worker under this same
interpreter, and tests/__init__.py points SPITBALL_ENGINE_DIR at a session
temp dir with nothing in it, so `installed()` is false throughout."""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from spitball import config, diarize, live_engine


def _cfg(**over):
    cfg = dict(config.DEFAULTS)
    cfg.update(over)
    return cfg


class TestExpectedAndGating(unittest.TestCase):
    def test_expected_far_speakers(self):
        meeting = {"attendees": [
            {"name": "A", "response": "accepted", "self": False},
            {"name": "B", "response": "declined", "self": False},
            {"name": "C", "response": "needs_action", "self": False},
            {"name": "", "response": "accepted", "self": True}]}
        self.assertEqual(diarize.expected_far_speakers(meeting), 2)
        self.assertIsNone(diarize.expected_far_speakers(None))
        self.assertIsNone(diarize.expected_far_speakers({"attendees": []}))
        self.assertIsNone(diarize.expected_far_speakers({"attendees": [{"self": True, "response": "accepted"}]}))
        self.assertIsNone(diarize.expected_far_speakers({"attendees": "garbage"}))

    def test_max_speakers_clamps(self):
        self.assertEqual(diarize.max_speakers(_cfg()), 6)
        self.assertEqual(diarize.max_speakers(_cfg(speaker_max=0)), 1)
        self.assertEqual(diarize.max_speakers(_cfg(speaker_max=99)), 12)
        self.assertEqual(diarize.max_speakers(_cfg(speaker_max="x")), 6)

    def test_skip_reasons(self):
        self.assertEqual(diarize.skip_reason(_cfg(speaker_split=False), None), "off")
        self.assertEqual(diarize.skip_reason(_cfg(speaker_max=1), None), "speaker_max is 1")
        self.assertEqual(diarize.skip_reason(_cfg(), 1), "one remote attendee expected")
        self.assertEqual(diarize.skip_reason(_cfg(), 3), "not installed")   # nothing installed in tests
        self.assertEqual(diarize.skip_reason(_cfg(), None), "not installed")
        with mock.patch("spitball.diarize.installed", return_value=True):
            self.assertEqual(diarize.skip_reason(_cfg(), 3), "")
            self.assertEqual(diarize.skip_reason(_cfg(), None), "")

    def test_clusters_for(self):
        self.assertEqual(diarize.clusters_for(_cfg(), 3), 3)
        self.assertEqual(diarize.clusters_for(_cfg(speaker_max=2), 5), 2)
        self.assertEqual(diarize.clusters_for(_cfg(), None), -1)
        self.assertEqual(diarize.clusters_for(_cfg(), 1), -1)

    def test_status_shape_when_nothing_installed(self):
        s = diarize.status()
        self.assertEqual(s["installed"], False)
        self.assertEqual(s["package"], False)
        self.assertEqual(s["models"], False)
        self.assertEqual(s["engine"], "sherpa-onnx")
        self.assertTrue(s["model_dir"].endswith("models/diarization"))


class TestInstalledOnDisk(unittest.TestCase):
    def test_installed_needs_package_and_both_models(self):
        with tempfile.TemporaryDirectory() as d, mock.patch("spitball.live_engine.ENGINE_DIR", Path(d)):
            self.assertFalse(diarize.installed())
            venv = Path(d) / "venv"
            (venv / "bin").mkdir(parents=True)
            (venv / "bin" / "python").write_text("")
            self.assertTrue(live_engine.installed())
            self.assertFalse(diarize.package_installed())
            (venv / "lib" / "python3.14" / "site-packages" / "sherpa_onnx").mkdir(parents=True)
            self.assertTrue(diarize.package_installed())
            self.assertFalse(diarize.installed())
            diarize.model_dir().mkdir(parents=True)
            diarize.segmentation_model().write_bytes(b"x")
            self.assertFalse(diarize.installed())
            diarize.embedding_model().write_bytes(b"y")
            self.assertTrue(diarize.installed())
            self.assertTrue(diarize.status()["installed"])


class TestSetup(unittest.TestCase):
    """setup(): the venv/pip step is `live_engine.install` with a mocked
    runner; downloads are a fake that writes bytes of our choosing."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        p = mock.patch("spitball.live_engine.ENGINE_DIR", self.tmp)
        p.start()
        self.addCleanup(p.stop)

    def _tarball(self, member_bytes: bytes) -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:bz2") as tar:
            info = tarfile.TarInfo(diarize.SEGMENTATION_MEMBER)
            info.size = len(member_bytes)
            tar.addfile(info, io.BytesIO(member_bytes))
            other = tarfile.TarInfo("sherpa-onnx-pyannote-segmentation-3-0/README.md")
            other.size = 3
            tar.addfile(other, io.BytesIO(b"hi\n"))
        return buf.getvalue()

    def _pins(self, seg_bytes: bytes, emb_bytes: bytes):
        tar_bytes = self._tarball(seg_bytes)
        return [
            mock.patch("spitball.diarize.SEGMENTATION_TAR_SHA256", hashlib.sha256(tar_bytes).hexdigest()),
            mock.patch("spitball.diarize.SEGMENTATION_SHA256", hashlib.sha256(seg_bytes).hexdigest()),
            mock.patch("spitball.diarize.EMBEDDING_SHA256", hashlib.sha256(emb_bytes).hexdigest()),
        ], tar_bytes

    def _downloader(self, tar_bytes: bytes, emb_bytes: bytes):
        calls = []

        def download(url, dst, timeout=600):
            calls.append(url)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(tar_bytes if url == diarize.SEGMENTATION_URL else emb_bytes)
        download.calls = calls
        return download

    def test_installs_package_then_models(self):
        run = mock.Mock(return_value=mock.Mock(returncode=0, stdout="", stderr=""))
        pins, tar_bytes = self._pins(b"SEG", b"EMB")
        download = self._downloader(tar_bytes, b"EMB")
        with mock.patch("spitball.live_engine.shutil.which", return_value="/usr/bin/uv"), \
             pins[0], pins[1], pins[2]:
            ok, message = diarize.setup(run=run, download=download)
        self.assertTrue(ok, message)
        cmds = [c.args[0] for c in run.call_args_list]
        self.assertEqual(cmds[0][:2], ["/usr/bin/uv", "venv"])
        self.assertEqual(cmds[1][:3], ["/usr/bin/uv", "pip", "install"])
        self.assertTrue(set(diarize.PACKAGES) <= set(cmds[1]))
        self.assertNotIn("onnx-asr[cpu]>=0.12,<0.13", cmds[1])  # the live engine's own packages are not touched
        self.assertEqual(download.calls, [diarize.SEGMENTATION_URL, diarize.EMBEDDING_URL])
        self.assertEqual(diarize.segmentation_model().read_bytes(), b"SEG")
        self.assertEqual(diarize.embedding_model().read_bytes(), b"EMB")
        self.assertFalse((diarize.model_dir() / "segmentation.tar.bz2").exists())
        self.assertIn("models in", message)

    def test_present_models_with_the_right_hash_are_not_downloaded_again(self):
        run = mock.Mock(return_value=mock.Mock(returncode=0, stdout="", stderr=""))
        pins, tar_bytes = self._pins(b"SEG", b"EMB")
        download = self._downloader(tar_bytes, b"EMB")
        diarize.model_dir().mkdir(parents=True)
        diarize.segmentation_model().write_bytes(b"SEG")
        diarize.embedding_model().write_bytes(b"EMB")
        with mock.patch("spitball.live_engine.shutil.which", return_value="/usr/bin/uv"), \
             pins[0], pins[1], pins[2]:
            ok, _ = diarize.setup(run=run, download=download)
        self.assertTrue(ok)
        self.assertEqual(download.calls, [])

    def test_wrong_hash_is_rejected_and_removed(self):
        run = mock.Mock(return_value=mock.Mock(returncode=0, stdout="", stderr=""))
        pins, tar_bytes = self._pins(b"SEG", b"EMB")
        download = self._downloader(tar_bytes, b"TAMPERED")
        with mock.patch("spitball.live_engine.shutil.which", return_value="/usr/bin/uv"), \
             pins[0], pins[1], pins[2]:
            ok, message = diarize.setup(run=run, download=download)
        self.assertFalse(ok)
        self.assertIn("hash mismatch", message)
        self.assertFalse(diarize.embedding_model().exists())
        self.assertTrue(diarize.segmentation_model().exists())  # the good one stays

    def test_tarball_without_the_member_fails(self):
        run = mock.Mock(return_value=mock.Mock(returncode=0, stdout="", stderr=""))
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:bz2") as tar:
            info = tarfile.TarInfo("something-else.txt")
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
        tar_bytes = buf.getvalue()
        download = self._downloader(tar_bytes, b"EMB")
        with mock.patch("spitball.live_engine.shutil.which", return_value="/usr/bin/uv"), \
             mock.patch("spitball.diarize.SEGMENTATION_TAR_SHA256", hashlib.sha256(tar_bytes).hexdigest()):
            ok, message = diarize.setup(run=run, download=download)
        self.assertFalse(ok)
        self.assertIn("no sherpa-onnx-pyannote-segmentation-3-0/model.int8.onnx inside", message)

    def test_pip_failure_stops_before_any_download(self):
        run = mock.Mock(return_value=mock.Mock(returncode=1, stdout="", stderr="no wheel for sherpa-onnx\n"))
        download = mock.Mock()
        with mock.patch("spitball.live_engine.shutil.which", return_value="/usr/bin/uv"):
            ok, message = diarize.setup(run=run, download=download)
        self.assertFalse(ok)
        self.assertEqual(message, "no wheel for sherpa-onnx")
        download.assert_not_called()

    def test_network_error_is_a_message_not_a_crash(self):
        import urllib.error
        run = mock.Mock(return_value=mock.Mock(returncode=0, stdout="", stderr=""))
        download = mock.Mock(side_effect=urllib.error.URLError("no route"))
        with mock.patch("spitball.live_engine.shutil.which", return_value="/usr/bin/uv"):
            ok, message = diarize.setup(run=run, download=download)
        self.assertFalse(ok)
        self.assertIn("download failed", message)

    def test_stdlib_venv_path_is_used_without_uv(self):
        run = mock.Mock(return_value=mock.Mock(returncode=0, stdout="", stderr=""))
        pins, tar_bytes = self._pins(b"SEG", b"EMB")
        with mock.patch("spitball.live_engine.shutil.which", return_value=None), pins[0], pins[1], pins[2]:
            ok, _ = diarize.setup(run=run, download=self._downloader(tar_bytes, b"EMB"))
        self.assertTrue(ok)
        cmds = [c.args[0] for c in run.call_args_list]
        self.assertEqual(cmds[0][:3], ["/usr/bin/python3", "-m", "venv"])
        self.assertIn("pip", cmds[1])


# ---------------------------------------------------------------- worker protocol

def _fake_worker(tmp: Path, body: str) -> Path:
    script = tmp / "fake_diarize_worker.py"
    script.write_text(textwrap.dedent('''
        import json, sys
        def say(o):
            sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
        args = sys.argv[1:]
    ''') + textwrap.dedent(body))
    return script


GOOD_WORKER = '''
    print("some library banner")
    say({"segments": [[0.0, 5.0, 0], [5.5, 9.0, 1], [9.0, 12.0, 0], [2.0, 1.0, 3]], "seconds": 0.4, "speakers": 2})
'''


class TestRun(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.wav = self.tmp / "far.wav"
        self.wav.write_bytes(b"")

    def _run(self, body, **kw):
        return diarize.run(self.wav, python=Path(sys.executable), script=_fake_worker(self.tmp, body),
                           timeout=kw.pop("timeout", 20), **kw)

    def test_parses_the_last_json_line_and_drops_bad_segments(self):
        r = self._run(GOOD_WORKER)
        self.assertEqual(r["segments"], [(0.0, 5.0, 0), (5.5, 9.0, 1), (9.0, 12.0, 0)])
        self.assertEqual(r["speakers"], 2)
        self.assertEqual(r["seconds"], 0.4)

    def test_arguments_reach_the_worker(self):
        body = '''
    say({"segments": [], "seconds": 0, "argv": args})
'''
        script = _fake_worker(self.tmp, body)
        r = subprocess.run([sys.executable, "-I", str(script), "seg", "emb", "w", "3", "0.700", "4", "0.3", "0.5"],
                           capture_output=True, text=True)
        self.assertEqual(json.loads(r.stdout)["argv"][3:], ["3", "0.700", "4", "0.3", "0.5"])
        # And diarize.run builds exactly that argv shape.
        with mock.patch("spitball.diarize.subprocess.run",
                        return_value=mock.Mock(returncode=0, stdout='{"segments": [], "seconds": 0}', stderr="")) as sr:
            diarize.run(self.wav, num_clusters=3, python=Path("/venv/python"), timeout=5)
        argv = sr.call_args[0][0]
        self.assertEqual(argv[:3], ["/venv/python", "-I", str(diarize.WORKER_SCRIPT)])
        self.assertEqual(argv[3:], [str(diarize.segmentation_model()), str(diarize.embedding_model()), str(self.wav),
                                    "3", "0.700", str(diarize.THREADS), "0.3", "0.5"])

    def test_worker_error_raises(self):
        with self.assertRaises(diarize.DiarizationError) as cm:
            self._run('say({"error": "ValueError: expected a 16 kHz WAV"}); sys.exit(1)\n')
        self.assertIn("16 kHz", str(cm.exception))

    def test_nonzero_exit_without_json_uses_stderr(self):
        with self.assertRaises(diarize.DiarizationError) as cm:
            self._run('sys.stderr.write("boom\\n"); sys.exit(3)\n')
        self.assertIn("boom", str(cm.exception))

    def test_timeout_raises(self):
        with self.assertRaises(diarize.DiarizationError) as cm:
            self._run("import time; time.sleep(30)\n", timeout=0.5)
        self.assertIn("timed out", str(cm.exception))

    def test_missing_python_raises(self):
        with self.assertRaises(diarize.DiarizationError):
            diarize.run(self.wav, python=Path("/no/such/python"), timeout=5)


class TestFarChannel(unittest.TestCase):
    def test_skipped_records_reason(self):
        segs, rec = diarize.far_channel(Path("/x.wav"), _cfg(), 1)
        self.assertEqual(segs, [])
        self.assertEqual(rec, {"ran": False, "engine": "sherpa-onnx", "expected": 1, "reason": "one remote attendee expected"})

    def test_success_caps_and_records(self):
        segs_in = [(0, 10, 0), (10, 20, 1), (20, 21, 2), (21, 30, 0)]
        with mock.patch("spitball.diarize.installed", return_value=True), \
             mock.patch("spitball.diarize.run", return_value={"segments": segs_in, "seconds": 1.23, "speakers": 3}) as r:
            segs, rec = diarize.far_channel(Path("/x.wav"), _cfg(speaker_max=2), 3)
        self.assertEqual(r.call_args[1]["num_clusters"], 2)  # expected 3, capped at speaker_max 2
        self.assertEqual(rec["ran"], True)
        self.assertEqual(rec["found"], 2)
        self.assertEqual(rec["num_clusters"], 2)
        self.assertEqual(rec["seconds"], 1.23)
        self.assertEqual(segs, [(0, 10, 0), (10, 20, 1), (20, 21, 1), (21, 30, 0)])  # 2 folded into nearest kept (1)

    def test_failure_falls_back_with_reason(self):
        with mock.patch("spitball.diarize.installed", return_value=True), \
             mock.patch("spitball.diarize.run", side_effect=diarize.DiarizationError("worker timed out after 120s")):
            segs, rec = diarize.far_channel(Path("/x.wav"), _cfg(), None)
        self.assertEqual(segs, [])
        self.assertFalse(rec["ran"])
        self.assertEqual(rec["reason"], "failed: worker timed out after 120s")
        self.assertEqual(rec["num_clusters"], -1)

    def test_no_speech(self):
        with mock.patch("spitball.diarize.installed", return_value=True), \
             mock.patch("spitball.diarize.run", return_value={"segments": [], "seconds": 0.1, "speakers": 0}):
            segs, rec = diarize.far_channel(Path("/x.wav"), _cfg(), None)
        self.assertEqual(segs, [])
        self.assertTrue(rec["ran"])
        self.assertEqual(rec["found"], 0)
        self.assertIn("no speech", rec["reason"])


# ---------------------------------------------------------------- interval math

SEGS = [(0.0, 10.0, 0), (10.0, 20.0, 1), (20.0, 20.4, 0), (20.4, 30.0, 1), (35.0, 40.0, 2)]


class TestIntervalMath(unittest.TestCase):
    def test_dominant_speaker(self):
        self.assertEqual(diarize.dominant_speaker(0, 10, SEGS), 0)
        self.assertEqual(diarize.dominant_speaker(8, 14, SEGS), 1)   # 2 s of 0, 4 s of 1
        self.assertEqual(diarize.dominant_speaker(8, 12, SEGS), 0)   # tie -> lower id
        self.assertIsNone(diarize.dominant_speaker(31, 34, SEGS))

    def test_plan_windows_cuts_at_a_speaker_change(self):
        self.assertEqual(diarize.plan_windows([(5.0, 15.0)], SEGS), [(5.0, 10.0, 0), (10.0, 15.0, 1)])

    def test_plan_windows_ignores_a_change_too_close_to_the_edge(self):
        # The 0.4 s blip at 20.0-20.4 never makes a piece of its own; the
        # window stays whole and goes to whoever spoke most of it.
        self.assertEqual(diarize.plan_windows([(19.5, 25.0)], SEGS), [(19.5, 25.0, 1)])

    def test_plan_windows_three_pieces(self):
        segs = [(0, 5, 0), (5, 10, 1), (10, 15, 2)]
        self.assertEqual(diarize.plan_windows([(0.0, 15.0)], segs), [(0.0, 5, 0), (5, 10, 1), (10, 15.0, 2)])

    def test_plan_windows_untouched_window_keeps_the_previous_speaker(self):
        self.assertEqual(diarize.plan_windows([(25.0, 29.0), (31.0, 34.0), (36.0, 39.0)], SEGS),
                         [(25.0, 29.0, 1), (31.0, 34.0, 1), (36.0, 39.0, 2)])
        self.assertEqual(diarize.plan_windows([(31.0, 34.0)], SEGS), [(31.0, 34.0, 0)])  # nothing before it: 0

    def test_plan_windows_no_segments(self):
        self.assertEqual(diarize.plan_windows([(0.0, 5.0)], []), [(0.0, 5.0, 0)])

    def test_cap_speakers(self):
        segs = [(0, 10, 0), (10, 20, 1), (20, 21, 2), (21, 30, 0), (40, 41, 3)]
        self.assertEqual(diarize.cap_speakers(segs, 2), [(0, 10, 0), (10, 20, 1), (20, 21, 1), (21, 30, 0), (40, 41, 0)])
        self.assertEqual(diarize.cap_speakers(segs, 4), segs)
        self.assertEqual(diarize.cap_speakers([], 2), [])

    def test_label_utterances_dominant_and_straddlers(self):
        utts = [{"channel": 0, "speaker": 0, "start": 0, "end": 2, "transcript": "me"},
                {"channel": 1, "speaker": 0, "start": 2, "end": 8, "transcript": "a"},
                {"channel": 1, "speaker": 0, "start": 8, "end": 16, "transcript": "straddles"},
                {"channel": 1, "speaker": 0, "start": 31, "end": 34, "transcript": "no segment"},
                {"channel": 1, "speaker": 0, "start": 36, "end": 39, "transcript": "c", "failed": True}]
        out, straddlers = diarize.label_utterances(utts, SEGS)
        self.assertEqual([u.get("speaker") for u in out], [0, 0, 1, 1, 0])
        self.assertEqual(straddlers, [(2, [(8, 10.0, 0), (10.0, 16, 1)])])
        self.assertEqual(utts[2]["speaker"], 0)  # input untouched


class TestSplitTranscript(unittest.TestCase):
    """split_transcript(): the reused-live-transcript path, with the far
    channel split, diarization, and re-transcription all faked."""

    def _normalized(self):
        return {"provider": "local", "model": "m", "utterances": [
            {"channel": 0, "speaker": 0, "start": 0, "end": 2, "transcript": "me"},
            {"channel": 1, "speaker": 0, "start": 2, "end": 8, "transcript": "a"},
            {"channel": 1, "speaker": 0, "start": 8, "end": 16, "transcript": "straddles"}]}

    def test_skip_records_reason_without_touching_audio(self):
        n = self._normalized()
        with mock.patch("spitball.audio.split_stereo_to_mono_wavs", side_effect=AssertionError("no")):
            out = diarize.split_transcript(Path("/a.opus"), n, _cfg(), 1)
        self.assertEqual(out["diarization"]["reason"], "one remote attendee expected")
        self.assertEqual([u["speaker"] for u in out["utterances"]], [0, 0, 0])

    def test_labels_and_resplits_straddlers(self):
        n = self._normalized()
        pieces_seen = []

        def transcribe_piece(wav, start, end, tmp, tag, speaker):
            pieces_seen.append((start, end, speaker))
            return [{"channel": 1, "speaker": speaker, "start": start, "end": end, "transcript": f"piece {speaker}"}]

        with mock.patch("spitball.diarize.installed", return_value=True), \
             mock.patch("spitball.audio.split_stereo_to_mono_wavs", return_value=(Path("/l.wav"), Path("/r.wav"))), \
             mock.patch("spitball.diarize.far_channel", return_value=(SEGS, {"ran": True, "found": 3})):
            out = diarize.split_transcript(Path("/a.opus"), n, _cfg(), 3, transcribe_piece=transcribe_piece)
        self.assertEqual(pieces_seen, [(8, 10.0, 0), (10.0, 16, 1)])
        self.assertEqual([(u["start"], u["speaker"], u["transcript"]) for u in out["utterances"]],
                         [(0, 0, "me"), (2, 0, "a"), (8, 0, "piece 0"), (10.0, 1, "piece 1")])
        self.assertEqual(out["diarization"]["resplit"], 1)

    def test_failed_resplit_keeps_the_original_utterance(self):
        n = self._normalized()

        def transcribe_piece(wav, start, end, tmp, tag, speaker):
            return [{"channel": 1, "speaker": speaker, "start": start, "end": end,
                     "transcript": "[transcription failed for this part]", "failed": True}]

        with mock.patch("spitball.diarize.installed", return_value=True), \
             mock.patch("spitball.audio.split_stereo_to_mono_wavs", return_value=(Path("/l.wav"), Path("/r.wav"))), \
             mock.patch("spitball.diarize.far_channel", return_value=(SEGS, {"ran": True, "found": 3})):
            out = diarize.split_transcript(Path("/a.opus"), n, _cfg(), 3, transcribe_piece=transcribe_piece)
        self.assertEqual([u["transcript"] for u in out["utterances"]], ["me", "a", "straddles"])
        self.assertEqual(out["utterances"][2]["speaker"], 1)  # still labeled by dominant overlap
        self.assertEqual(out["diarization"]["resplit"], 0)

    def test_without_a_transcriber_only_labels(self):
        n = self._normalized()
        with mock.patch("spitball.diarize.installed", return_value=True), \
             mock.patch("spitball.audio.split_stereo_to_mono_wavs", return_value=(Path("/l.wav"), Path("/r.wav"))), \
             mock.patch("spitball.diarize.far_channel", return_value=(SEGS, {"ran": True, "found": 3})):
            out = diarize.split_transcript(Path("/a.opus"), n, _cfg(), 3)
        self.assertEqual([u["speaker"] for u in out["utterances"]], [0, 0, 1])
        self.assertEqual(out["diarization"]["resplit"], 0)

    def test_ffmpeg_failure_is_a_reason(self):
        n = self._normalized()
        with mock.patch("spitball.diarize.installed", return_value=True), \
             mock.patch("spitball.audio.split_stereo_to_mono_wavs",
                        side_effect=subprocess.CalledProcessError(1, "ffmpeg")):
            out = diarize.split_transcript(Path("/a.opus"), n, _cfg(), 3)
        self.assertFalse(out["diarization"]["ran"])
        self.assertIn("failed:", out["diarization"]["reason"])
        self.assertEqual([u["speaker"] for u in out["utterances"]], [0, 0, 0])


# ---------------------------------------------------------------- the real thing (gated)

REAL_DIR = os.environ.get("SPITBALL_DIARIZE_TEST_DIR", "")
CACHE = Path(__file__).resolve().parent / ".cache"


def _two_voice_fixture(dst: Path) -> bool:
    """Builds a two-speaker 16 kHz mono WAV from the two different real
    speech clips the noise A/B left in tests/.cache/ (never shipped):
    speaker A 0-6 s, B 6.8-11.8, A 12.6-18.6, B 19.4-24.4, A 25.2-31.2,
    B 32-37. False when the clips or ffmpeg aren't there."""
    a, b = CACHE / "spacewalk.wav", CACHE / "bueller.wav"
    if not (a.exists() and b.exists() and shutil.which("ffmpeg")):
        return False
    graph = ("[0:a]aformat=channel_layouts=mono,aresample=16000,asplit=3[a0][a1][a2];"
             "[1:a]aformat=channel_layouts=mono,aresample=16000,asplit=3[b0][b1][b2];"
             "[a0]atrim=0:6,asetpts=PTS-STARTPTS[A1];[b0]atrim=5:10,asetpts=PTS-STARTPTS[B1];"
             "[a1]atrim=6:12,asetpts=PTS-STARTPTS[A2];[b1]atrim=10:15,asetpts=PTS-STARTPTS[B2];"
             "[a2]atrim=12:18,asetpts=PTS-STARTPTS[A3];[b2]atrim=12.5:17.5,asetpts=PTS-STARTPTS[B3];"
             + "".join(f"anullsrc=r=16000:cl=mono,atrim=0:0.8[g{i}];" for i in range(1, 6))
             + "[A1][g1][B1][g2][A2][g3][B2][g4][A3][g5][B3]concat=n=11:v=0:a=1[out]")
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(a), "-i", str(b),
                        "-filter_complex", graph, "-map", "[out]", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
                        str(dst)], capture_output=True, text=True)
    return r.returncode == 0


@unittest.skipUnless(REAL_DIR, "SPITBALL_DIARIZE_TEST_DIR not set (a dir with venv/ holding sherpa-onnx + "
                               "models/diarization/ with the two pinned models)")
class TestRealDiarization(unittest.TestCase):
    """The real worker on real two-voice audio. Needs an engine dir laid
    out like `spitball diarize setup` leaves it, and the two speech clips
    in tests/.cache/. Runs in a few seconds."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        p = mock.patch("spitball.live_engine.ENGINE_DIR", Path(REAL_DIR))
        p.start()
        self.addCleanup(p.stop)
        if not diarize.installed():
            self.skipTest(f"{REAL_DIR} has no venv with sherpa-onnx or is missing the models")
        self.wav = self.tmp / "two-voices.wav"
        if not _two_voice_fixture(self.wav):
            self.skipTest("tests/.cache/spacewalk.wav + bueller.wav (or ffmpeg) not available")

    def _dominant(self, segments, s, e):
        return diarize.dominant_speaker(s, e, segments)

    def test_two_voices_with_known_count(self):
        r = diarize.run(self.wav, num_clusters=2)
        self.assertEqual(r["speakers"], 2)
        segs = r["segments"]
        a = [self._dominant(segs, s, e) for s, e in ((0.5, 5.5), (13.0, 18.0), (25.7, 30.7))]
        b = [self._dominant(segs, s, e) for s, e in ((7.3, 11.3), (19.9, 23.9), (32.5, 36.5))]
        self.assertEqual(len(set(a)), 1, f"speaker A stretches should share one id: {a}")
        self.assertTrue(all(x is not None for x in a), a)
        self.assertTrue(any(x is not None and x != a[0] for x in b), f"speaker B should get the other id: {b}")

    def test_two_voices_with_unknown_count(self):
        r = diarize.run(self.wav, num_clusters=-1)
        self.assertGreaterEqual(r["speakers"], 2)
        self.assertLessEqual(r["speakers"], 3)

    def test_far_channel_end_to_end_records_the_run(self):
        segs, rec = diarize.far_channel(self.wav, _cfg(), 2)
        self.assertTrue(rec["ran"], rec)
        self.assertEqual(rec["num_clusters"], 2)
        self.assertEqual(rec["found"], 2)
        self.assertGreater(rec["seconds"], 0)
        self.assertTrue(segs)


if __name__ == "__main__":
    unittest.main()
