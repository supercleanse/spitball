"""`spitball` CLI. `spitball daemon` runs the service; everything else is a thin client
over the control socket (see CONTRACT.md), or a local read/write against config.json
for the `config`/`check`/`local`/`pick-folder` commands (the settings panel's whole
backend surface)."""
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
  reprocess <call-dir> [--retranscribe] [--event <id> | --no-event]
                             redo transcription + summary for one call; --event/--no-event
                             pin or clear its calendar match by hand
  config get [--json]       show effective settings (secrets masked)
  config set <key> <value>  set one setting (JSON-typed)
  config set-secret <key>   set a secret from stdin (deepgram_api_key, summary_api_key,
                             calendar_ics_url)
  config unset <key>        remove a setting override, back to its default
  check transcription [--provider P] [--json]
                             test the configured (or given) transcription provider
  check summary [--json]    test the summary endpoint
  calendar test [--at TIME] [--app APP] [--meet CODE] [--refresh] [--json]
                             which calendar event a call starting now (or at TIME:
                             "14:30", "2026-09-30 14:30", ISO 8601, or epoch) would match
  calendar upcoming [--hours N] [--refresh] [--json]
                             the meeting reminders due in the next N hours (24): each
                             event with a video link, its link host, and when it fires
  local info [--json]       what voxtype is currently configured with
  local models [--json]     every whisper/parakeet model voxtype knows about
  local set-model <name>    switch voxtype to that model in the background (progress in model.json)
  live setup                install the live transcript's fast engine (a small venv with onnx-asr)
  live status [--json]      whether the fast engine is installed and which model it would load
  speakers <call-dir> [--json]
                             list the far-side speakers of one call and who they resolved to
  speakers <call-dir> <n> "Name" | --clear
                             name speaker n by hand (or go back to automatic) and re-render
                             transcript.md, summary.md, and the export copy
  diarize setup             install the on-device speaker split (sherpa-onnx + two small
                             models, into the live-engine venv)
  diarize status [--json]   whether the speaker split is installed
  pick-folder [--title T]   native folder chooser; prints the chosen path
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
        return {"ok": False, "error": f"daemon not reachable ({e}); is the Spitball plugin enabled? Restart it with `omarchy-shell supercleanse.spitball restart`"}
    finally:
        s.close()


def _open(path: str) -> None:
    subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def _parse_value(raw: str):
    """JSON-typed: true/false/numbers (and null/lists/objects) parsed as
    JSON; anything that isn't valid JSON (a plain unquoted string, which is
    the common case -- e.g. a folder path) passes through as-is."""
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _flag_value(args: list, flag: str) -> str | None:
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args):
            return args[i + 1]
    return None


def _config_cmd(rest: list) -> int:
    sub = rest[0] if rest else ""
    args = rest[1:]

    if sub == "get":
        cfg = config.load()
        out = dict(cfg)
        for key in config.SECRET_KEYS:
            out[key] = config.secret_status(cfg, key)
        print(json.dumps(out, indent=1))
        return 0

    if sub == "set":
        if len(args) < 2:
            print(USAGE)
            return 2
        key, raw_value = args[0], args[1]
        if key in config.SECRET_KEYS:
            print(f"{key!r} is a secret -- use `spitball config set-secret {key}` instead", file=sys.stderr)
            return 1
        if key not in config.DEFAULTS:
            print(f"unknown config key: {key!r}", file=sys.stderr)
            return 1
        config.set_key(key, _parse_value(raw_value))
        send("reload")
        return 0

    if sub == "set-secret":
        if not args:
            print(USAGE)
            return 2
        key = args[0]
        if key not in config.SECRET_KEYS:
            print(f"not a secret key: {key!r} (expected one of {', '.join(config.SECRET_KEYS)})",
                  file=sys.stderr)
            return 1
        value = sys.stdin.readline().strip()  # never argv; ONE line so a caller that never closes the pipe (the QML settings UI) cannot hang us. Empty line/EOF clears it
        config.set_key(key, value)
        send("reload")
        return 0

    if sub == "unset":
        if not args:
            print(USAGE)
            return 2
        key = args[0]
        if key not in config.DEFAULTS:
            print(f"unknown config key: {key!r}", file=sys.stderr)
            return 1
        config.unset_key(key)
        send("reload")
        return 0

    print(USAGE)
    return 2


def _check_cmd(rest: list) -> int:
    sub = rest[0] if rest else ""
    cfg = config.load()

    if sub == "transcription":
        provider = _flag_value(rest, "--provider")
        from . import providers
        try:
            result = providers.check(cfg, provider)
        except RuntimeError as e:
            result = {"ok": False, "message": str(e)}
        print(json.dumps(result))
        return 0 if result.get("ok") else 1

    if sub == "summary":
        from . import process
        result = process.check_summary(cfg)
        print(json.dumps(result))
        return 0 if result.get("ok") else 1

    print(USAGE)
    return 2


def _local_cmd(rest: list) -> int:
    sub = rest[0] if rest else ""
    from .providers import local

    if sub == "info":
        print(json.dumps(local.info()))
        return 0

    if sub == "models":
        print(json.dumps(local.list_models()))
        return 0

    if sub == "set-model":
        args = rest[1:]
        if not args:
            print(USAGE)
            return 2
        result = local.set_model(args[0])
        if result.get("ok"):
            print(result.get("message", "ok"))
            return 0
        print(result.get("message", "couldn't switch models"), file=sys.stderr)
        return 1

    # Internal: the detached child `set_model()` re-execs itself into. Not
    # part of the documented CLI (see CONTRACT.md) -- never run this
    # directly; it blocks until the switch finishes or fails.
    if sub == "_set-model-worker":
        args = rest[1:]
        if not args:
            print(USAGE)
            return 2
        local.run_set_model_worker(args[0])
        return 0

    # Internal: bin/spitball-upgrade-parakeet's streaming step, so the
    # terminal fallback configures streaming exactly like the worker does.
    if sub == "_apply-streaming":
        args = rest[1:]
        if not args:
            print(USAGE)
            return 2
        error = local.apply_streaming_config(args[0])
        if error:
            print(error, file=sys.stderr)
            return 1
        return 0

    print(USAGE)
    return 2


def _calendar_cmd(rest: list) -> int:
    sub = rest[0] if rest else ""
    from . import calendar

    if sub == "test":
        cfg = config.load()
        try:
            at = calendar.parse_at(_flag_value(rest, "--at") or "now")
        except (ValueError, TypeError) as e:
            print(f"bad --at value ({e}); use \"14:30\", \"2026-09-30 14:30\", ISO 8601, or epoch seconds",
                  file=sys.stderr)
            return 2
        meet = _flag_value(rest, "--meet")
        rep = calendar.test_report(cfg, at, app=_flag_value(rest, "--app") or "",
                                   meet_codes=[meet.lower()] if meet else [], refresh="--refresh" in rest)
        if "--json" in rest:
            print(json.dumps(rep))
        else:
            print(calendar.format_test_report(rep))
        return 0 if rep.get("ok") else 1

    if sub == "upcoming":
        from . import reminders
        cfg = config.load()
        hours_arg = _flag_value(rest, "--hours")
        try:
            hours = float(hours_arg) if hours_arg is not None else 24.0
            if not 0 < hours <= 24 * 14:
                raise ValueError("out of range")
        except ValueError as e:
            print(f"bad --hours value ({e}); use a number of hours up to 336", file=sys.stderr)
            return 2
        rep = reminders.upcoming_report(cfg, hours=hours, refresh="--refresh" in rest)
        if "--json" in rest:
            print(json.dumps(rep))
        else:
            print(reminders.format_upcoming_report(rep))
        return 0 if rep.get("ok") else 1

    print(USAGE)
    return 2


def _live_cmd(rest: list) -> int:
    sub = rest[0] if rest else ""
    from . import live_engine
    from .providers import local

    if sub == "setup":
        print("Installing the live transcript engine (onnx-asr + onnxruntime)...")
        ok, message = live_engine.setup()
        print(message, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1

    if sub == "status":
        model_dir = live_engine.model_dir_for(local.info())
        status = {"installed": live_engine.installed(),
                  "venv": str(live_engine.venv_python().parent.parent),
                  "model": model_dir.name if model_dir else "",
                  "fast": live_engine.installed() and model_dir is not None}
        if "--json" in rest:
            print(json.dumps(status))
        elif status["fast"]:
            print(f"Fast live transcript: on ({status['model']})")
        elif not status["installed"]:
            if live_engine.venv_present():
                print(f"Fast live transcript: not installed -- the venv at {status['venv']} has no "
                      "onnx-asr (the speaker split shares it); run `spitball live setup` to add it")
            else:
                print("Fast live transcript: not installed (run `spitball live setup`)")
        else:
            print("Fast live transcript: installed, but voxtype isn't on a Parakeet model")
        return 0

    print(USAGE)
    return 2


def _diarize_cmd(rest: list) -> int:
    sub = rest[0] if rest else ""
    from . import diarize

    if sub == "setup":
        print("Installing the speaker split (sherpa-onnx into the live-engine venv, then two models)...")
        ok, message = diarize.setup()
        print(message, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1

    if sub == "status":
        status = diarize.status()
        if "--json" in rest:
            print(json.dumps(status))
        elif status["installed"]:
            print(f"Speaker split: installed ({status['engine']}, models in {status['model_dir']})")
        elif status["package"]:
            print("Speaker split: sherpa-onnx is installed but the models are missing (run `spitball diarize setup`)")
        else:
            print("Speaker split: not installed (run `spitball diarize setup`)")
        return 0

    print(USAGE)
    return 2


def _speakers_cmd(rest: list) -> int:
    args = [a for a in rest if a not in ("--json", "--clear")]
    if not args:
        print(USAGE)
        return 2
    from . import process, speakers
    call_dir = Path(args[0]).expanduser().resolve()
    try:
        if len(args) == 1 and "--clear" not in rest:
            rows = process.list_speakers(call_dir)
        else:
            if len(args) < 2 or not args[1].isdigit() or (len(args) < 3 and "--clear" not in rest):
                print(USAGE)
                return 2
            name = "" if "--clear" in rest else args[2]
            rows = process.rename_speaker(call_dir, int(args[1]), name)
    except (RuntimeError, ValueError, OSError) as e:
        print(str(e), file=sys.stderr)
        return 1
    if "--json" in rest:
        print(json.dumps(rows))
    else:
        print(speakers.format_listing(rows))
    return 0


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
        retranscribe = "--retranscribe" in rest
        event_id = _flag_value(rest, "--event")
        no_event = "--no-event" in rest
        skip = {"--retranscribe", "--no-event", "--event"}
        dirs = [a for i, a in enumerate(rest) if a not in skip and (i == 0 or rest[i - 1] != "--event")]
        if not dirs or (event_id is not None and no_event):
            print(USAGE)
            return 2
        from . import process
        call_dir = Path(dirs[0]).expanduser().resolve()
        if event_id is not None or no_event:
            process.set_calendar_override(call_dir, None if no_event else event_id)
        r = process.process(call_dir, {}, notify=print, retranscribe=retranscribe)
        print(f"{r['title']}\n{r['summary']}")
        return 0
    if cmd == "config":
        return _config_cmd(rest)
    if cmd == "check":
        return _check_cmd(rest)
    if cmd == "calendar":
        return _calendar_cmd(rest)
    if cmd == "local":
        return _local_cmd(rest)
    if cmd == "live":
        return _live_cmd(rest)
    if cmd == "diarize":
        return _diarize_cmd(rest)
    if cmd == "speakers":
        return _speakers_cmd(rest)
    if cmd == "pick-folder":
        title = _flag_value(rest, "--title") or "Choose a folder"
        from . import pickfolder
        code, path = pickfolder.pick_folder(title)
        if code == 0:
            print(path)
        return code
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
