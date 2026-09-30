# Spitball: daemon ↔ bar widget contract

The recorder daemon (`spitball daemon`) owns all logic. The Omarchy bar widget is a
thin view: it reads one state file and runs CLI commands.

`SpitballService.qml` (the plugin's `service` entry point) launches and supervises
the daemon as `/usr/bin/python3 -I <plugin-dir>/bin/spitball daemon`, restarting it
with exponential backoff (capped at 60s) if it exits, and sending it `SIGTERM` on
shutdown so an in-progress recording gets a clean chance to finish. `Widget.qml` (the
`bar-widget` entry point) never starts or stops the daemon itself — it only reads
`state.json` and shells out through `bin/spitball`. Plugin id: `supercleanse.spitball`.

## State file

`$XDG_RUNTIME_DIR/spitball/state.json` (normally `/run/user/1000/spitball/state.json`).
Written atomically (temp file + rename) on every change, and at least once a second
while recording. If the file is missing or unparseable, treat it as `{"state": "offline"}`.

```json
{
  "state": "idle",
  "app": "",
  "started_at": 0,
  "auto_record": false,
  "message": "",
  "last_call": null,
  "setup_needed": "",
  "updated_at": 1790000000
}
```

| field | meaning |
|:--|:--|
| `state` | `offline` (daemon not running), `idle`, `detected` (an app opened the mic; not recording yet), `recording`, `processing` (transcribing/summarizing the last call), `error` |
| `app` | display name of the app on the call, e.g. `"Zoom"`, `"Chrome"`, `"Slack"`; `""` if none/manual |
| `started_at` | epoch seconds when recording started; `0` unless `recording` |
| `auto_record` | when true, the daemon starts recording on detection without asking |
| `message` | short human text for the tooltip, e.g. `"Transcribing…"`, `"Deepgram error: 401"` |
| `last_call` | `null` or `{"dir": "...", "title": "Weekly sync", "ended_at": 1790000000, "summary": "..."}` |
| `setup_needed` | `""` when ready to transcribe, else a short reason (`"Add a Deepgram key"`, `"Install dictation (voxtype) or pick a cloud service"`). Computed at daemon start and after every `reload` -- cheap checks only (key present / voxtype on PATH), never a network call. Detection and recording still work while setup is needed; processing fails with this same reason. |
| `updated_at` | epoch seconds of the last write |

`auto_record` lives in `config.json` (see below), not a separate persisted
flag -- a daemon upgrading from before this migrated the old
`~/.local/state/spitball/persist.json` value into `config.json` once, the
first time it started after the upgrade.

`processing` can overlap a new call: if a new call is detected while the previous one
is still processing, `state` becomes `detected`/`recording` (the live call wins) and
processing continues in the background.

## Model switch file

`$XDG_RUNTIME_DIR/spitball/model.json` — written by a background `spitball local
set-model <name>` (see the CLI table below and `spitball/providers/local.py`), read
by both the settings popup (when reopened) and the bar widget (while it's running).
Completely independent of state.json/the states above: a model switch never appears
as a `state` value there, and the daemon doesn't write this file at all. Missing or
unparseable means "no switch running" — never an error condition of its own. Written
atomically, same as state.json.

```json
{
  "name": "parakeet-tdt-0.6b-v3-int8",
  "state": "downloading",
  "message": "Downloading parakeet-tdt-0.6b-v3-int8…",
  "done_bytes": 41943040,
  "total_bytes": 671088640
}
```

| field | meaning |
|:--|:--|
| `name` | the voxtype model slug being switched to |
| `state` | `switching-engine` (pkexec step, only when the engine actually changes) → `downloading` → `activating` → `restarting` (only if the voxtype user unit is active) → `done`. Or `error` (see `message`) or `terminal` (fell back to an interactive terminal — see below; no further progress is reported here) |
| `message` | short human text, e.g. `"Downloading parakeet-tdt-0.6b-v3-int8…"`, `"Password prompt canceled"` |
| `done_bytes` / `total_bytes` | download progress; both `0` outside the `downloading` state or before the first progress event arrives |

The bar widget never lets this override a real call state (recording/detected/
processing/error) — it only ever shows in the same slot `setup_needed` already
occupies (idle/offline, nothing more important going on). See Widget.qml's
`showModelSwitch`.

## Runtime and state paths

| Path | Contents |
|:--|:--|
| `$XDG_RUNTIME_DIR/spitball/state.json` | The state file above. |
| `$XDG_RUNTIME_DIR/spitball/model.json` | The model switch file above. |
| `$XDG_RUNTIME_DIR/spitball/ctl.sock` | The Unix control socket (mode `0600`). |
| `~/.local/state/spitball/persist.json` | Durable bits that survive a daemon restart: `last_call`. (`auto_record` lived here before it moved into `config.json`; a pre-existing value migrates over once, then this file stops carrying it.) |
| `~/.config/spitball/config.json` | User settings (optional; see README's Configuration section for every key). |

All three env vars `SPITBALL_RUNTIME_DIR`, `SPITBALL_STATE_DIR`, `SPITBALL_CONFIG`
override the corresponding path — used by the test suite so it never touches a live
daemon's files.

## Control socket

A length-prefix-free, one-shot JSON request/response over
`$XDG_RUNTIME_DIR/spitball/ctl.sock`: connect, send `{"cmd": ..., "arg": ...}`,
shut down the write side, read until EOF, parse the reply as JSON. Every reply has
at least `{"ok": bool}`, plus `error` on failure or command-specific fields on
success (`status` echoes the full state plus `present`, the list of call apps
currently holding the mic).

## CLI

`bin/spitball` is the plugin's own CLI, invoked by the widget as
`/usr/bin/python3 -I <plugin-dir>/bin/spitball <command>` (absolute path, isolated
mode, no reliance on `PATH`). It resolves its own location, so it also works
symlinked onto `PATH` (see the README) or run as `python3 -I bin/spitball <command>`
from a checkout.

| command | effect |
|:--|:--|
| `spitball start` | start recording now (uses the detected app if any, else manual) |
| `spitball stop` | stop recording; processing begins |
| `spitball toggle` | start if not recording, else stop |
| `spitball dismiss` | ignore the current detection (back to `idle` until the app releases and re-opens the mic), or clear an `error` |
| `spitball auto on\|off\|toggle` | set `auto_record` |
| `spitball open-last` | open the last call's `summary.md` |
| `spitball open-folder` | open the calls folder (`calls_dir`, default `~/Calls`) |
| `spitball status [--json]` | print the state |
| `spitball reprocess <call-dir> [--retranscribe]` | redo transcription + summary for one call folder. Reuses the cached transcript (`.transcript.json`, or an old folder's `.deepgram.json`) unless `--retranscribe` is given, which calls the provider again. |
| `spitball config get [--json]` | effective settings (defaults merged with `config.json`). Each `*_api_key` is masked to `{"set": bool, "source": "config"\|"env"\|"command"\|"none"}` -- the raw value is never printed. |
| `spitball config set <key> <value>` | sets one setting. Value is JSON-typed (`true`/`false`/numbers parsed; anything else stays a plain string). Unknown keys and the three secret keys (see `set-secret`) are rejected. Preserves every other key already in `config.json`, known or not. Sends `reload` to the daemon afterward (ignored if it's down). |
| `spitball config set-secret <key>` | reads the value from **stdin**, never argv, for `deepgram_api_key` / `summary_api_key` (phase 2 adds more as new providers land). Empty stdin clears it. Sends `reload`. |
| `spitball config unset <key>` | removes a key, back to its default. Sends `reload`. |
| `spitball check transcription [--provider P] [--json]` | `{"ok": bool, "message": "…"}` -- tests the configured (or given) transcription provider for real: deepgram does an authenticated `GET /v1/projects`; local checks voxtype is on PATH. |
| `spitball check summary [--json]` | `{"ok": bool, "message": "…", "models": [...]}` from `GET {summary_base_url}/models`. |
| `spitball local info [--json]` | what voxtype is actually configured with right now: `{"installed": bool, "engine": "whisper"\|"parakeet"\|"", "model": "...", "onnx": bool, "can_upgrade_parakeet": bool, "message": "..."}`. Spitball never manages this itself -- it's read straight from `voxtype config get`. |
| `spitball local models [--json]` | every whisper/parakeet model voxtype knows how to download (from `voxtype info models --json`), each `{"name", "engine", "installed", "size_mb", "languages", "recommended", "active"}`. Spitball never downloads any of these -- see `set-model`. |
| `spitball local set-model <name>` | starts switching voxtype to `name` **in the background** and returns immediately -- see "Model switch file" above. Normally: a detached child re-execs `spitball local _set-model-worker <name>` after a graphical `pkexec voxtype setup onnx --enable/--disable` prompt (only if the engine is actually changing) and `voxtype setup --download --model <name> --activate --progress-format json`, whose NDJSON events feed model.json directly, then, for a parakeet model, `voxtype config set parakeet.streaming true|false` (true only for a streaming-capable model, which also gets the three `streaming_*_secs` window sizes written into voxtype's `[parakeet]` table if missing). Falls back to opening a floating terminal running `bin/spitball-upgrade-parakeet` (the original interactive approach) when `pkexec` is missing or Omarchy's shell doesn't answer `shell ping` (no way to draw a graphical prompt) -- model.json then gets `state: "terminal"` and no further progress. Exit 1 only if neither path could be started at all (no pkexec/agent AND no terminal launcher). Spitball's own process never runs `sudo` or `pkexec` itself, or edits voxtype's config directly. |
| `spitball local _set-model-worker <name>` | **internal** -- the blocking worker `set-model` spawns detached; never run this directly. |
| `spitball local _apply-streaming <name>` | **internal** -- the streaming step on its own (see `set-model`), so `bin/spitball-upgrade-parakeet`'s terminal fallback configures streaming exactly like the background worker. Exit 1 with a message on stderr if voxtype refuses the setting. |
| `spitball live setup` | creates `~/.local/share/spitball/live-engine/venv` (uv when available, else `python3 -m venv` + pip) with onnx-asr + onnxruntime + sentencepiece for the live engine. Exit 1 with the installer's last error line on stderr. |
| `spitball live status [--json]` | `{"installed": bool, "venv": "...", "model": "<voxtype parakeet model or empty>", "fast": bool}` -- `fast` means the next recording's live transcript will use the engine. |
| `spitball pick-folder [--title T]` | native folder chooser (xdg-desktop-portal → zenity → kdialog); prints the chosen absolute path. Exit 1 if canceled, exit 2 if no picker is available at all (caller should fall back to a text field). |
| `spitball daemon` | run the service (`SpitballService.qml` does this; you shouldn't need to) |

`spitball reload` (a thin CLI wrapper over the `reload` control-socket
command, sent automatically by every `config` write above) makes the daemon
re-read `config.json` and recompute `setup_needed`.

All commands except `daemon` return within ~1s; they talk to the daemon over the
control socket above, and report `daemon not reachable` if nothing answers.

## Widget behavior (Omarchy bar, center section, immediately left of `omarchy.indicators`)

- `idle` / `offline`: collapsed like an inactive indicator. Zero width, revealed only
  while the center section is hover-revealed, the same way inactive indicators are. When
  revealed: a dim record glyph. Exception: when `state` is
  `idle` and `setup_needed` is non-empty, the widget stays visible (not collapsed) as a
  small gear/"Set up" glyph, tooltip = `setup_needed`, and the menu leads with
  **Set up transcription…** (opens Settings at Transcription).
  Settings itself stays reachable from the menu in every state. A second,
  independent exception: while `state` is `idle`/`offline` and model.json (see "Model
  switch file" above) says a `spitball local set-model` switch is in flight or just
  failed, the widget stays visible as a small download glyph, tooltip = the switch's
  status text (e.g. "Downloading Parakeet v3 (int8)… 42%"); if the switch failed, the
  menu leads with **Set up transcription…**. Neither exception ever overrides
  `detected`/`recording`/`processing`/`error` below -- those always take priority.
- `detected`: always visible. Amber record glyph plus the app name (`Zoom`), gently
  pulsing. Tooltip "Call detected in Zoom: click for options".
- `recording`: always visible. Red dot plus elapsed `m:ss` / `h:mm:ss` computed from
  `started_at`, ticking every second. Tooltip "Recording Zoom: click for options".
- `processing`: always visible, neutral glyph, tooltip = `message`.
- `error`: always visible, red glyph, tooltip = `message`.
- Left or right click in any state → a small popup menu (the widget's whole UI):
  Set up transcription… (only when setup is needed or a model switch failed) ·
  Start recording (when not recording; also opens the Live popup) · Show live
  transcript + Stop recording (only while recording) · Dismiss (only when `detected`) ·
  Auto-record on detect ✓ · Open last summary (disabled if `last_call` is null; show
  its title) · Open calls folder · Settings…. A bar click never stops a recording by
  itself: a user once clicked the dot expecting a menu and killed their own call
  mid-conversation, so Stop is only ever an explicit menu item or the Live popup's
  button.

## Live popup

A third bar dropdown popup (same `KeyboardPanel` component as the menu and Settings
above), opened by the menu's Start recording / Show live transcript items and closable
with its header ✕, Esc, or an outside click without affecting the recording. Header:
app name, red dot, elapsed time (from `started_at`), a **Stop recording** button (the
only way to stop from this popup), and the ✕. The Settings popup has the same ✕ in a
header pinned above its scrolling form. Body: a chat-style transcript fed by `live.json` below -- my lines
(channel 0) on the right, theirs (channel 1) on the left, a speaker label only when the
speaker changes, small `m:ss` timestamps, auto-scroll to the newest bubble only while
already at the bottom (otherwise a "New messages ↓" pill). Status line at the bottom:
"Listening…"/"Catching up…" while `state` is `recording`, or once it leaves `recording`,
"Recording saved; transcribing…" plus a **Close** button (may auto-close after a few
seconds).

## Live transcript file

`$XDG_RUNTIME_DIR/spitball/live.json` -- written by a background live-transcriber
thread (`spitball/live.py`) the daemon starts alongside the recorder and stops
alongside it (never more than one at a time, never more than one `voxtype` process
running at once). Independent of state.json; the widget reads both. Written
atomically, same as state.json. Kept (status `stopped`) after the recording ends, until
the next recording starts.

```json
{
  "call_id": "2026-09-28-1400-zoom",
  "started_at": 1790000000,
  "status": "listening",
  "message": "",
  "utterances": [
    {"channel": 0, "start": 1.2, "end": 4.8, "transcript": "hey, thanks for hopping on"},
    {"channel": 1, "start": 5.3, "end": 6.9, "transcript": "sure, so the", "partial": true}
  ]
}
```

| field | meaning |
|:--|:--|
| `call_id` | the call folder's name |
| `started_at` | epoch seconds, same value as state.json's `started_at` for this call |
| `status` | `listening` (keeping up), `catching-up` (more than ~20s of closed audio waiting to be transcribed), `unavailable` (`live_transcript` is off, or voxtype isn't installed -- see `message`), or `stopped` (the recording ended; the file is kept as-is) |
| `message` | only meaningful for `unavailable` -- e.g. `"Live transcript needs voxtype"` |
| `utterances` | `{"channel", "start", "end", "transcript", "failed"?, "partial"?}`, sorted by `start` -- same per-utterance shape a transcription provider returns (see `spitball/providers/__init__.py`). `partial: true` marks the line still being spoken (at most one per channel, only with the live engine -- see below): its text is replaced on every update and it becomes a normal utterance once a pause closes it. Partials never reach the call folder's `.live.json`. |

On stop, the live thread also writes the call folder's `<call-dir>/.live.json` (the
normalized provider shape, `{"provider": "local", "model": ..., "utterances": [...],
"note": "live transcript"}`) -- see `spitball/process.py`'s `process()`: if
`transcription_provider` is `local`, it's used as the transcript instead of
transcribing the file again, unless it's missing, empty, or more than 10% of the
call's spoken time is marked `failed`, in which case a normal full transcription runs.
The `deepgram` provider always transcribes the full file, regardless of `.live.json`.
`spitball reprocess --retranscribe` ignores `.live.json` the same way it ignores
`.transcript.json`.

Two new `config.json` settings (defaults in `spitball/config.py`, editable via
`spitball config set`, documented in README.md's Configuration table):

| key | default | meaning |
|:--|:--|:--|
| `live_transcript` | `true` | Run the live transcriber while recording. Always uses the `local` (voxtype) provider, regardless of `transcription_provider` -- it never costs money or sends audio anywhere mid-call. |
| `live_max_window_s` | `12` | A still-open segment (no pause yet) is cut here regardless, so one long uninterrupted stretch of talk still produces a line before the call ends. |
| `live_engine` | `true` | Use the persistent live engine when `spitball live setup` has installed it and voxtype is on a Parakeet model: the model stays loaded for the call (spitball/engine_worker.py under `~/.local/share/spitball/live-engine/venv`), ticks run every 0.5s instead of 2s, and the open segment is published as a `partial`. `false`, or any engine failure, uses per-segment `voxtype transcribe` as before. |
