#!/usr/bin/env python3
"""A stand-in for bin/spitball used ONLY by tests/offscreen/render.sh.

Answers the read-only commands the settings overlay issues with canned
JSON, accepts every write (`config set` / `set-secret` / `unset`) without
touching anything, and reports "no picker" for pick-folder. Nothing here is
reachable from the real plugin: Widget.qml resolves bin/spitball by its own
location, and this file is never on that path. Run it only through
render.sh's --fake-data flag.
"""
import json
import os
import sys

SETTINGS = {
    "calls_dir": "/home/demo/Calls",
    "export_dir": "/home/demo/Notes/Calls",
    "my_name": "Demo",
    "auto_record": True,
    "language": "en",
    "transcription_provider": "local",
    "deepgram_api_key": {"set": False, "source": "none"},
    "deepgram_api_key_command": "",
    "deepgram_model": "nova-3",
    "summary_base_url": "http://127.0.0.1:11434/v1",
    "summary_model": "qwen3.6:35b-a3b",
    "summary_api_key": {"set": True, "source": "command"},
    "summary_api_key_command": "cat ~/.config/spitball/summary-key",
    "summary_enabled": True,
    "call_apps": {"zoom": "Zoom", "chrome": "Chrome", "chromium": "Chromium", "brave": "Brave",
                  "firefox": "Firefox", "slack": "Slack", "teams": "Teams", "discord": "Discord",
                  "signal": "Signal", "webex": "Webex", "whatsapp": "WhatsApp"},
    "live_transcript": True,
    "live_max_window_s": 12,
    "live_engine": True,
    "detect_after_s": 2,
    "end_after_s": 8,
    "min_call_s": 60,
    "min_manual_s": 10,
    "opus_bitrate": "32k",
}

LOCAL_INFO = {"installed": True, "engine": "parakeet", "model": "parakeet-unified-en-0.6b",
              "onnx": True, "can_upgrade_parakeet": False, "message": ""}

LOCAL_MODELS = [
    {"name": "parakeet-unified-en-0.6b", "engine": "parakeet", "installed": True, "size_mb": 2400,
     "languages": "English", "recommended": True, "active": True},
    {"name": "parakeet-tdt-0.6b-v3-int8", "engine": "parakeet", "installed": False, "size_mb": 640,
     "languages": "25 European languages", "recommended": False, "active": False},
    {"name": "base.en", "engine": "whisper", "installed": True, "size_mb": 142,
     "languages": "English", "recommended": False, "active": False},
]

SUMMARY_CHECK = {"ok": True, "message": "3 models", "models": ["qwen3.6:35b-a3b", "llama3.1:8b", "gemma3:12b"]}
LIVE_STATUS = {"installed": True, "venv": "/home/demo/.local/share/spitball/live-engine/venv",
               "model": "parakeet-unified-en-0.6b", "fast": True}
STATUS = {"ok": True, "state": "idle", "app": "", "started_at": 0, "auto_record": True,
          "message": "", "last_call": {"title": "Weekly sync", "dir": "/home/demo/Calls/x"},
          "setup_needed": "", "updated_at": 0, "present": ["Zoom"]}


def main(argv):
    log = os.environ.get("SPITBALL_FAKE_LOG")
    if log:
        with open(log, "a") as fh:
            fh.write(" ".join(argv) + "\n")
    cmd = argv[0] if argv else ""
    rest = argv[1:]
    if cmd == "config":
        sub = rest[0] if rest else ""
        if sub == "get":
            print(json.dumps(SETTINGS))
            return 0
        if sub in ("set", "unset"):
            return 0
        if sub == "set-secret":
            sys.stdin.read()
            return 0
        return 2
    if cmd == "local":
        sub = rest[0] if rest else ""
        if sub == "info":
            print(json.dumps(LOCAL_INFO)); return 0
        if sub == "models":
            print(json.dumps(LOCAL_MODELS)); return 0
        if sub == "set-model":
            return 0
        return 2
    if cmd == "check":
        sub = rest[0] if rest else ""
        if sub == "summary":
            print(json.dumps(SUMMARY_CHECK)); return 0
        if sub == "transcription":
            print(json.dumps({"ok": True, "message": "voxtype is installed"})); return 0
        return 2
    if cmd == "live":
        sub = rest[0] if rest else ""
        if sub == "status":
            print(json.dumps(LIVE_STATUS)); return 0
        return 0
    if cmd == "status":
        print(json.dumps(STATUS)); return 0
    if cmd == "pick-folder":
        return 2
    if cmd in ("open-folder", "open-last", "reload"):
        return 0
    print(f"fake spitball: unknown command {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
