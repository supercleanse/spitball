"""The Spitball daemon: detection state machine, recording, processing, and
the control socket the CLI and bar widget talk to. Runs as the `spitball` systemd
user service. See CONTRACT.md for the state file the widget reads.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import threading
import time
import traceback
from pathlib import Path

from . import config, detect, process
from .recorder import Recording


def notify(title: str, body: str = "", urgency: str = "normal") -> None:
    try:
        subprocess.Popen(["notify-send", "-a", "Spitball", "-u", urgency, title, body])
    except OSError:
        pass


class Daemon:
    def __init__(self):
        self.cfg = config.load()
        self.lock = threading.RLock()
        self.state = "idle"
        self.app = ""
        self.message = ""
        self.rec: Recording | None = None
        self.rec_dir: Path | None = None
        self.rec_origin = ""          # "detected" or "manual"
        self.present: set = set()     # call apps holding the mic right now
        self.first_seen: dict = {}    # app -> time first seen this session
        self.last_seen: dict = {}     # app -> time last seen
        self.dismissed: set = set()
        self.processing = 0
        self.error = ""
        self._shutting_down = False   # set once run()'s finally starts; blocks further publishes
        persist = self._load_persist()
        self.auto_record = bool(persist.get("auto_record", False))
        self.last_call = persist.get("last_call")
        self.rescan = threading.Event()

    # ---------------------------------------------------------------- state I/O

    def _load_persist(self) -> dict:
        try:
            return json.loads(config.PERSIST_FILE.read_text())
        except (OSError, ValueError):
            return {}

    def _save_persist(self):
        config.atomic_write(config.PERSIST_FILE, json.dumps(
            {"auto_record": self.auto_record, "last_call": self.last_call}, indent=1))

    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "app": self.app,
            "started_at": int(self.rec.started_at) if self.rec else 0,
            "auto_record": self.auto_record,
            "message": self.message,
            "last_call": self.last_call,
            "updated_at": int(time.time()),
        }

    def publish(self):
        # Once shutdown has started, the final "offline" write (below, in
        # run()'s finally) must be the last word -- a background _process()
        # thread that's still finishing up a call must not clobber it with a
        # late "processing"/"idle"/"error" write.
        if self._shutting_down:
            return
        config.atomic_write(config.STATE_FILE, json.dumps(self.snapshot()))

    def _set(self, state: str, message: str = ""):
        self.state, self.message = state, message
        self.publish()

    def _idle_state(self):
        """What to show when no call is live."""
        if self.error:
            self._set("error", self.error)
        elif self.processing:
            self._set("processing", self.message if self.state == "processing" else "Transcribing…")
        else:
            self.app = ""
            self._set("idle")

    # ---------------------------------------------------------------- recording

    def start(self, origin: str = "manual") -> dict:
        with self.lock:
            if self.rec:
                return {"ok": True, "note": "already recording"}
            self.error = ""
            if origin == "manual" and self.present:
                origin = "detected"  # clicking Record on a detected call = follow that call
            self.app = sorted(self.present)[0] if origin == "detected" and self.present else ""
            self.rec_origin = origin
            self.rec_dir = process.new_call_dir(self.cfg, self.app)
            self.rec = Recording(self.rec_dir / "audio.opus", self.cfg["opus_bitrate"])
            time.sleep(0.3)
            if not self.rec.alive():
                err = (self.rec_dir / "ffmpeg.log").read_text()[-300:] if (self.rec_dir / "ffmpeg.log").exists() else ""
                self.rec = None
                process.discard(self.rec_dir)
                self.error = f"Recorder failed to start: {err.strip() or 'ffmpeg exited'}"
                self._set("error", self.error)
                notify("Spitball failed to start", self.error, "critical")
                return {"ok": False, "error": self.error}
            self._set("recording", f"Recording {self.app or 'audio'}")
            return {"ok": True}

    def stop(self) -> dict:
        with self.lock:
            if not self.rec:
                return {"ok": True, "note": "not recording"}
            rec, call_dir, origin, app = self.rec, self.rec_dir, self.rec_origin, self.app
            self.rec = None
            duration = rec.stop()
            if self.present:
                self.dismissed |= self.present  # don't re-prompt for the call we just ended
            minimum = self.cfg["min_call_s"] if origin == "detected" else self.cfg["min_manual_s"]
            if duration < minimum:
                process.discard(call_dir)
                notify("Recording discarded", f"Shorter than {int(minimum)} seconds.")
                self._idle_state()
                return {"ok": True, "note": "discarded (too short)"}
            meta = {"app": app, "started_at": rec.started_at, "duration": duration}
            # Written before processing starts, so a restart mid-processing can resume it.
            (call_dir / ".meta.json").write_text(json.dumps(meta))
            self.processing += 1
            self.message = "Transcribing…"
            threading.Thread(target=self._process, args=(call_dir, meta), daemon=True).start()
            self._idle_state()
            return {"ok": True}

    def _process(self, call_dir: Path, meta: dict):
        def progress(msg):
            with self.lock:
                self.message = msg
                if self.state == "processing":
                    self.publish()
        try:
            result = process.process(call_dir, meta, self.cfg, notify=progress)
            with self.lock:
                self.last_call = result
                self._save_persist()
            notify(f"Call saved: {result['title']}", result["summary"])
        except Exception as e:
            traceback.print_exc()
            with self.lock:
                self.error = f"{e} (audio kept in {call_dir.name})"
            notify("Call processing failed", self.error, "critical")
        finally:
            with self.lock:
                self.processing -= 1
                if self.state in ("processing", "error", "idle"):
                    self._idle_state()

    # ---------------------------------------------------------------- detection

    def tick(self):
        now = time.time()
        apps = detect.call_apps(self.cfg["call_apps"])
        with self.lock:
            for a in apps:
                self.first_seen.setdefault(a, now)
                self.last_seen[a] = now
            # An app is "present" once it has held the mic for detect_after_s, and stays
            # present until it has been gone for end_after_s (rides out device switches).
            present = {a for a, t in self.last_seen.items()
                       if now - t < self.cfg["end_after_s"]
                       and now - self.first_seen.get(a, now) >= self.cfg["detect_after_s"]}
            for a in list(self.last_seen):
                if now - self.last_seen[a] >= self.cfg["end_after_s"]:
                    self.last_seen.pop(a, None)
                    self.first_seen.pop(a, None)
                    self.dismissed.discard(a)
            self.present = present

            if self.rec:
                if not self.rec.alive():
                    self.error = "Recorder stopped unexpectedly"
                    notify("Spitball stopped unexpectedly", str(self.rec_dir), "critical")
                    self.stop()
                elif self.rec_origin == "detected" and not present:
                    self.stop()  # the call ended
                    notify("Call ended", "Recording stopped. Transcribing now.")
                else:
                    self.publish()  # keep updated_at fresh for the widget
                return

            live = present - self.dismissed
            if live:
                if self.auto_record:
                    self.start("detected")
                    notify(f"Recording {self.app}", "Auto-record is on.")
                elif self.state != "detected":
                    self.app = sorted(live)[0]
                    self._set("detected", f"Call detected in {self.app}")
                    notify(f"Call detected in {self.app}", "Click the red dot in the bar to record.")
                elif self.app not in live:
                    self.app = sorted(live)[0]
                    self._set("detected", f"Call detected in {self.app}")
            elif self.state == "detected":
                self._idle_state()

    # ---------------------------------------------------------------- control

    def handle(self, cmd: str, arg: str = "") -> dict:
        with self.lock:
            if cmd == "start":
                return self.start("manual")
            if cmd == "stop":
                return self.stop()
            if cmd == "toggle":
                return self.stop() if self.rec else self.start("manual")
            if cmd == "dismiss":
                self.dismissed |= self.present
                if self.error:
                    self.error = ""
                if not self.rec:
                    self._idle_state()
                return {"ok": True}
            if cmd == "auto":
                self.auto_record = {"on": True, "off": False}.get(arg, not self.auto_record)
                self._save_persist()
                self.publish()
                return {"ok": True, "auto_record": self.auto_record}
            if cmd == "status":
                return {"ok": True, **self.snapshot(), "present": sorted(self.present)}
            if cmd == "reload":
                self.cfg = config.load()
                return {"ok": True}
        return {"ok": False, "error": f"unknown command {cmd!r}"}

    def serve(self):
        config.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        try:
            config.SOCKET_PATH.unlink()
        except FileNotFoundError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(config.SOCKET_PATH))
        os.chmod(config.SOCKET_PATH, 0o600)
        srv.listen(8)
        while True:
            conn, _ = srv.accept()
            with conn:
                try:
                    conn.settimeout(5)
                    req = json.loads(conn.recv(4096).decode() or "{}")
                    reply = self.handle(req.get("cmd", ""), req.get("arg", ""))
                except Exception as e:
                    reply = {"ok": False, "error": str(e)}
                try:
                    conn.sendall(json.dumps(reply).encode())
                except OSError:
                    pass

    def recover(self):
        """Finish anything a previous run left behind: stop a recorder that outlived
        its daemon (killed shell), then process calls that never got a summary."""
        try:
            out = subprocess.run(["pgrep", "-f", f"ffmpeg .*-name {detect.OWN_APP_NAME} "],
                                 capture_output=True, text=True, timeout=5).stdout.split()
            for pid in out:
                os.kill(int(pid), signal.SIGINT)
            if out:
                time.sleep(3)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        calls = Path(self.cfg["calls_dir"]).expanduser()
        if not calls.is_dir():
            return
        for d in sorted(calls.iterdir()):
            audio = d / "audio.opus"
            if not d.is_dir() or not audio.exists() or (d / "summary.md").exists():
                continue
            meta_path = d / ".meta.json"
            if meta_path.exists():
                meta = json.loads(meta_path.read_text())
            else:  # the daemon died mid-recording: rebuild the facts from the file
                duration = process.audio_seconds(audio)
                if duration < self.cfg["min_manual_s"]:
                    process.discard(d)
                    continue
                meta = {"app": "", "started_at": audio.stat().st_mtime - duration, "duration": duration}
                meta_path.write_text(json.dumps(meta))
            with self.lock:
                self.processing += 1
                self.message = "Transcribing…"
            threading.Thread(target=self._process, args=(d, meta), daemon=True).start()
        with self.lock:
            if not self.rec:
                self._idle_state()

    def run(self):
        self.publish()
        self.recover()
        threading.Thread(target=self.serve, daemon=True, name="ctl").start()
        detect.Watcher(self.rescan.set).start()
        try:
            while True:
                try:
                    self.tick()
                except Exception:
                    traceback.print_exc()
                self.rescan.wait(1)
                self.rescan.clear()
        finally:
            if self.rec:
                self.stop()
            self._shutting_down = True
            config.atomic_write(config.STATE_FILE, json.dumps({"state": "offline"}))


def main():
    import signal
    d = Daemon()
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(SystemExit(0)))
    d.run()
