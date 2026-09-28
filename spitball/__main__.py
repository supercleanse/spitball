"""`spitball` CLI. `spitball daemon` runs the service; everything else is a thin client
over the control socket (see CONTRACT.md)."""
from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path

from . import config

USAGE = """usage: spitball <command>
  start | stop | toggle     control recording
  dismiss                   ignore the current call detection (or clear an error)
  auto on|off|toggle        record automatically when a call is detected
  open-last                 open the last call's summary
  open-folder               open the calls folder
  status [--json]           show the current state
  reprocess <call-dir>      redo transcription + summary for one call
  daemon                    run the service (systemd does this)"""


def send(cmd: str, arg: str = "") -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(15)
    try:
        s.connect(str(config.SOCKET_PATH))
        s.sendall(json.dumps({"cmd": cmd, "arg": arg}).encode())
        s.shutdown(socket.SHUT_WR)
        data = b""
        while chunk := s.recv(65536):
            data += chunk
        return json.loads(data or b"{}")
    except (OSError, ValueError) as e:
        return {"ok": False, "error": f"daemon not reachable ({e}); is `spitball.service` running?"}
    finally:
        s.close()


def _open(path: str) -> None:
    subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "daemon":
        from .daemon import main as run
        run()
        return 0
    if cmd == "reprocess":
        if not rest:
            print(USAGE)
            return 2
        from . import process
        r = process.process(Path(rest[0]).expanduser().resolve(), {}, notify=print)
        print(f"{r['title']}\n{r['summary']}")
        return 0
    if cmd == "open-folder":
        d = Path(config.load()["calls_dir"]).expanduser()
        d.mkdir(parents=True, exist_ok=True)
        _open(str(d))
        return 0
    if cmd == "open-last":
        last = send("status").get("last_call")
        if last and Path(last["summary"]).exists():
            _open(last["summary"])
            return 0
        print("no calls yet")
        return 1
    if cmd == "status":
        r = send("status")
        if "--json" in rest:
            print(json.dumps(r, indent=1))
        else:
            line = r.get("state", "offline")
            if r.get("app"):
                line += f" ({r['app']})"
            if r.get("message"):
                line += f": {r['message']}"
            print(line if r.get("ok") else r.get("error"))
        return 0 if r.get("ok") else 1
    if cmd in ("start", "stop", "toggle", "dismiss", "auto", "reload"):
        r = send(cmd, rest[0] if rest else "")
        if not r.get("ok"):
            print(r.get("error"), file=sys.stderr)
            return 1
        if r.get("note"):
            print(r["note"])
        return 0
    print(USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main())
