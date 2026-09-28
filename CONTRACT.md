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
| `updated_at` | epoch seconds of the last write |

`processing` can overlap a new call: if a new call is detected while the previous one
is still processing, `state` becomes `detected`/`recording` (the live call wins) and
processing continues in the background.

## Runtime and state paths

| Path | Contents |
|:--|:--|
| `$XDG_RUNTIME_DIR/spitball/state.json` | The state file above. |
| `$XDG_RUNTIME_DIR/spitball/ctl.sock` | The Unix control socket (mode `0600`). |
| `~/.local/state/spitball/persist.json` | Durable bits that survive a daemon restart: `auto_record`, `last_call`. |
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
| `spitball reprocess <call-dir>` | redo transcription + summary for one call folder |
| `spitball daemon` | run the service (`SpitballService.qml` does this; you shouldn't need to) |

All commands except `daemon` return within ~1s; they talk to the daemon over the
control socket above, and report `daemon not reachable` if nothing answers.

## Widget behavior (Omarchy bar, center section, immediately left of `omarchy.indicators`)

- `idle` / `offline`: collapsed like an inactive indicator. Zero width, revealed only
  while the center section is hover-revealed, the same way inactive indicators are. When
  revealed: a dim record glyph; click → `spitball start`.
- `detected`: always visible. Amber record glyph plus the app name (`Zoom`), gently
  pulsing. Tooltip "Call detected in Zoom: click to record". Left click → `spitball start`.
- `recording`: always visible. Red dot plus elapsed `m:ss` / `h:mm:ss` computed from
  `started_at`, ticking every second. Tooltip "Recording Zoom: click to stop". Left
  click → `spitball stop`.
- `processing`: always visible, neutral glyph, tooltip = `message`. No click action.
- `error`: always visible, red glyph, tooltip = `message`. Click → `spitball open-folder`.
- Right click in any state → a small popup menu (the widget's whole UI):
  Start/Stop recording · Dismiss (only when `detected`) · Auto-record on detect ✓ ·
  Open last summary (disabled if `last_call` is null; show its title) · Open calls folder.
