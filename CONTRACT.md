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
| `~/.local/share/spitball/live-engine/venv/`, `models/diarization/` | The optional venv (`spitball live setup` / `spitball diarize setup`) and the speaker split's two models (`pyannote-segmentation-3.0.int8.onnx`, `3dspeaker-eres2net-en-voxceleb.onnx`), downloaded from the k2-fsa GitHub releases and verified by SHA-256 (`models/diarization/README.md`). |
| `~/.local/state/spitball/calendar/feed.ics`, `feed.json` | The cached calendar feed (mode `0600`, directory `0700`) and its metadata (`fetched_at`, `etag`, `last_modified`, `bytes`, and `key`, a SHA-256 prefix of the feed URL -- never the URL itself). See "Calendar events" below. |

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
| `spitball reprocess <call-dir> [--retranscribe] [--event <id> \| --no-event]` | redo transcription + summary for one call folder. Reuses the cached transcript (`.transcript.json`, or an old folder's `.deepgram.json`) unless `--retranscribe` is given, which calls the provider again with the current settings (including `mic_denoise`, see "Mic noise reduction" below); hand-set speaker names (`source: user`) are carried onto the fresh transcript by provider speaker id, and any that can't be are printed, noted in the headers, and recorded under `speakers_dropped` (see "Transcript cache"). `--event <id>` pins the calendar match to one of the snapshot's candidates (ids as listed in `.meta.json` / `calendar test --json`); `--no-event` clears it. Either is stored as `calendar.override` in `.meta.json` and honored by every later run. |
| `spitball config get [--json]` | effective settings (defaults merged with `config.json`). Each `*_api_key` is masked to `{"set": bool, "source": "config"\|"env"\|"command"\|"none"}` -- the raw value is never printed. |
| `spitball config set <key> <value>` | sets one setting. Value is JSON-typed (`true`/`false`/numbers parsed; anything else stays a plain string). Unknown keys and the three secret keys (see `set-secret`) are rejected. Preserves every other key already in `config.json`, known or not. Sends `reload` to the daemon afterward (ignored if it's down). |
| `spitball config set-secret <key>` | reads the value from **stdin**, never argv, for `deepgram_api_key` / `summary_api_key` / `calendar_ics_url` (phase 2 adds more as new providers land). Empty stdin clears it. Sends `reload`. |
| `spitball config unset <key>` | removes a key, back to its default. Sends `reload`. |
| `spitball check transcription [--provider P] [--json]` | `{"ok": bool, "message": "…"}` -- tests the configured (or given) transcription provider for real: deepgram does an authenticated `GET /v1/projects`; local checks voxtype is on PATH. |
| `spitball check summary [--json]` | `{"ok": bool, "message": "…", "models": [...]}` from `GET {summary_base_url}/models`. |
| `spitball calendar test [--at TIME] [--app APP] [--meet CODE] [--refresh] [--json]` | the calendar source's health plus the match for a call starting now (or at `TIME`: `"14:30"`, `"2026-09-30 14:30"`, ISO 8601, or epoch seconds). `--app` / `--meet` supply what a real call would have (the app on the mic, a Meet code from a window title); `--refresh` re-downloads the feed regardless of its age. Works whether or not `calendar_enabled` is on. `--json`: `{"ok", "enabled", "source": "ics"\|"command"\|"off", "message", "error", "events_nearby", "match": <event>\|null, "confident", "confidence", "candidates": [{"id", "title", "start", "end", "score", "filtered", "reasons"}], "summary", "at", "fetched_at", "cached", "my_email", "my_email_known", "rules_skipped"}`. `ok` is about the source (fetched, or served from the cache); no match is still `ok`. `my_email_known` is false when the feed's owner couldn't be told (see "Owner" under Calendar events); `rules_skipped` counts recurring series the expander refused (see "Recurrence"). Exit 1 when the source fails or nothing is configured -- **the JSON is still printed**, and the settings overlay reads it (a nonzero exit with JSON on stdout is an answer, not a crash). `message`/`error` never contain the feed address: a malformed one is reported as `feed address must start with https:// or webcal://` and every fetch error is scrubbed of it. |
| `spitball local info [--json]` | what voxtype is actually configured with right now: `{"installed": bool, "engine": "whisper"\|"parakeet"\|"", "model": "...", "onnx": bool, "can_upgrade_parakeet": bool, "message": "..."}`. Spitball never manages this itself -- it's read straight from `voxtype config get`. |
| `spitball local models [--json]` | every whisper/parakeet model voxtype knows how to download (from `voxtype info models --json`), each `{"name", "engine", "installed", "size_mb", "languages", "recommended", "active"}`. Spitball never downloads any of these -- see `set-model`. |
| `spitball local set-model <name>` | starts switching voxtype to `name` **in the background** and returns immediately -- see "Model switch file" above. Normally: a detached child re-execs `spitball local _set-model-worker <name>` after a graphical `pkexec voxtype setup onnx --enable/--disable` prompt (only if the engine is actually changing) and `voxtype setup --download --model <name> --activate --progress-format json`, whose NDJSON events feed model.json directly, then, for a parakeet model, `voxtype config set parakeet.streaming true|false` (true only for a streaming-capable model, which also gets the three `streaming_*_secs` window sizes written into voxtype's `[parakeet]` table if missing). Falls back to opening a floating terminal running `bin/spitball-upgrade-parakeet` (the original interactive approach) when `pkexec` is missing or Omarchy's shell doesn't answer `shell ping` (no way to draw a graphical prompt) -- model.json then gets `state: "terminal"` and no further progress. Exit 1 only if neither path could be started at all (no pkexec/agent AND no terminal launcher). Spitball's own process never runs `sudo` or `pkexec` itself, or edits voxtype's config directly. |
| `spitball local _set-model-worker <name>` | **internal** -- the blocking worker `set-model` spawns detached; never run this directly. |
| `spitball local _apply-streaming <name>` | **internal** -- the streaming step on its own (see `set-model`), so `bin/spitball-upgrade-parakeet`'s terminal fallback configures streaming exactly like the background worker. Exit 1 with a message on stderr if voxtype refuses the setting. |
| `spitball live setup` | creates `~/.local/share/spitball/live-engine/venv` (uv when available, else `python3 -m venv` + pip) with onnx-asr + onnxruntime + sentencepiece for the live engine. Into an existing venv (one `diarize setup` made) it installs the same packages rather than skipping. Exit 1 with the installer's last error line on stderr. |
| `spitball live status [--json]` | `{"installed": bool, "venv": "...", "model": "<voxtype parakeet model or empty>", "fast": bool}` -- `installed` means the venv exists **and** onnx-asr is in it (checked on disk under its site-packages, never by importing), so a venv holding only the speaker split's sherpa-onnx reads `false` and the plain output says the venv has no onnx-asr; `fast` means the next recording's live transcript will use the engine. |
| `spitball speakers <call-dir> [--json]` | the call's far-side speakers from its cached `.transcript.json`: `[{"n", "label", "id", "name", "confidence", "source", "evidence", "seconds", "words"}]` -- `n` is the label number ("Speaker n"; `label` is "Them" when there is one voice), `id` the provider's speaker id, `source` `user` \| `calendar` \| `llm` \| `""`. Exit 1 when the folder has no cached transcript. |
| `spitball speakers <call-dir> <n> "Name"` \| `--clear` | records a hand-set name (`source: "user"`, kept by every later run) or clears it back to automatic in the `speakers` block, then re-renders `transcript.md`, `summary.md` (speaker labels inside the summary's own text are rewritten), and the `export_dir` copy from the cached transcript -- no transcription, no model call. Prints the new listing. Exit 1 for an unknown speaker number. |
| `spitball diarize setup` | installs the on-device speaker split: the `sherpa-onnx` wheel (plus numpy) into `~/.local/share/spitball/live-engine/venv` (created if missing; the live engine's own packages are untouched), then the two models above, each verified by hash (a mismatch is deleted and reported). Exit 1 with the reason on stderr. |
| `spitball diarize status [--json]` | `{"installed": bool, "package": bool, "models": bool, "venv": "...", "model_dir": "...", "engine": "sherpa-onnx"}` -- `installed` means both the wheel and both models are in place; `package` is sherpa-onnx's own presence in the shared venv, independent of the live engine's (a live-engine-only venv reads `false` here). |
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
  **Set up transcription…** (opens Settings on its Transcription page).
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

## Call folder metadata

`<call-dir>/.meta.json` is written by the daemon at record start (`app`,
`started_at`), completed at stop (`duration`), and updated by `process()` (`titled`
once the folder has been renamed, and the calendar decision). `spitball reprocess`
and crash recovery read it back; a start-only file (the daemon died mid-call) gets its
`duration` from the audio file.

```json
{
  "app": "Chrome",
  "started_at": 1790000000.0,
  "duration": 1802.4,
  "titled": true,
  "calendar": {
    "source": "ics",
    "fetched_at": 1790000001,
    "cached": true,
    "error": "",
    "app": "Chrome",
    "started_at": 1790000000.0,
    "meet_codes": ["abc-defg-hij"],
    "events": [ ...normalized events whose time touches the call... ],
    "match": {"id": "…", "title": "Weekly sync", "confidence": 170, "confident": true},
    "override": {"event": "…"}
  }
}
```

`calendar` is present only when the calendar was on (at record start, or at a later
`process()`/`reprocess` when no snapshot existed yet). `events` is the candidate
snapshot: every event (see "Calendar events" below) whose span runs from 15 minutes
before `started_at` to 10 minutes after it, captured once so `reprocess` and offline
runs see the same picture; `meet_codes` are the Google Meet codes visible in window
titles at that moment (`hyprctl clients -j`, read-only; titles themselves are never
stored); `error` is a short reason when the source failed (the recording is never
affected); `match` is the decision the last `process()` made; `override` exists only
after `reprocess --event/--no-event`.

## Transcript cache

`<call-dir>/.transcript.json` is the normalized provider shape (`{"provider",
"model", "utterances": [...]}`, see `spitball/providers/__init__.py`), cached so
`reprocess` never re-transcribes unless asked. When a call has a confident calendar
match, `process()` adds a `meeting` block (and removes it again when a later run has
no match), for the speaker-naming phase and anything else that wants to know who was
on the invite:

```json
{
  "provider": "local", "model": "…", "utterances": [ ... ],
  "mic_denoise": {
    "mode": "auto", "applied": true, "filter": "arnndn",
    "noise_floor_db": -38.2, "speech_level_db": -21.0, "threshold_db": -45.0
  },
  "meeting": {
    "id": "weekly-sync@google.com/2026-09-30T14:00:00-06:00",
    "title": "Weekly sync",
    "start": "2026-09-30T14:00:00-06:00",
    "end": "2026-09-30T14:30:00-06:00",
    "organizer": {"name": "Alex Demo", "email": "alex@example.com"},
    "attendees": [
      {"name": "Alex Demo", "email": "alex@example.com", "response": "accepted", "self": false, "optional": false},
      {"name": "", "email": "me@example.com", "response": "accepted", "self": true, "optional": false}
    ],
    "conference": {"kind": "meet", "url": "https://meet.google.com/abc-defg-hij", "code": "abc-defg-hij"},
    "confidence": 170
  }
}
```

`attendees` includes you (`self: true`, when the feed identifies you) so a consumer
can subtract yourself to get the far side; `response` is `accepted` / `declined` /
`tentative` / `needs_action`; `name` may be empty when the invite carries only an
address. Speaker naming reads this block and never rewrites it.

**What reaches the summary endpoint.** The summarizer's metadata is built apart from
the local `transcript.md`/`summary.md` header (`process._model_header()`): date, app,
length, then `calendar.summary_context()` -- the meeting title and time always, the
invite list (`**People on the invite:**`, names only) only when
`calendar_names_to_summary`, the description only when
`calendar_description_to_summary` -- then the header notes. The local
`**Attendees:**` line is never part of it. The speaker-naming request
(`speaker_names`, and nothing else, gates it) carries the neutral-label transcript
and each invitee's name; never an email address -- a nameless invitee is sent as
`Invitee N (no name on the invite)` and mapped back locally.

**`speakers`** (spitball/speakers.py) is who each far-side voice is, written by
`process()` after transcription and by `spitball speakers`:

```json
{
  "speakers": {
    "1": {"id": 2, "name": "Priya Nair", "confidence": "high", "source": "llm",
          "evidence": "00:00:42 'thanks, Priya' from the next speaker"},
    "2": {"id": 5, "name": "Alex Demo", "confidence": "medium", "source": "llm", "evidence": "…"},
    "3": {"id": 7, "name": "", "confidence": "none", "source": "", "evidence": ""}
  },
  "diarization": {"ran": true, "engine": "sherpa-onnx", "expected": 3, "num_clusters": 3,
                  "found": 3, "seconds": 4.1}
}
```

Keys are the label numbers as rendered (`Speaker 1`, `Speaker 2`, …; with a single
far voice the one key is `"1"`, rendered "Them"), ordered by first appearance;
`id` is the provider's own speaker id for that label, which is how an entry follows
its voice across a reprocess. Before labels are assigned, far-side ids with under 5
seconds and under 12 words fold into the id speaking nearest to them, and ids
beyond `speaker_max` fold the same way; the utterances themselves are never
rewritten. `confidence` is `high` / `medium` / `low` / `none`; `source` is `user`
(set by `spitball speakers`, never overwritten), `calendar` (a 1:1: one other
non-declined invitee and one far voice), `llm` (the naming call), or `""`. Rendering:
`user` or `high` → the name; `medium` → `Speaker 2 (probably Alex Demo)`; anything
else → the bare label. Every run recomputes the non-`user` entries (an entry whose
`id` is gone is dropped); a naming failure leaves them empty and adds a
`**Note:** speaker names unavailable: …` header line. The block is absent when the
call has no far-side speech.

`reprocess --retranscribe` carries the `user` entries onto the fresh transcript by
`id` (the provider's speaker id, or the local split's cluster id). One whose id is
not among the new far-side ids can't be placed: it is listed under
**`speakers_dropped`** (`[{"name": "Alex D.", "id": 5, "was": "Speaker 2"}]`), the
command prints it, and the headers carry `**Note:** re-transcribing changed the
far-side voices, so 1 hand-set name could not be carried over: …`. The record stays
through plain reprocesses until `spitball speakers` sets that name again or the next
`--retranscribe` replaces it. A plain `reprocess` never writes it.

**`diarization`** is written by the local provider (or by `process()` when it
reused `.live.json`): whether the on-device split ran. `ran: false` carries a
`reason` (`off`, `speaker_max is 1`, `one remote attendee expected`, `not
installed`, `no speech found on the far channel`, or `failed: …`); `expected` is
the invitee count from the meeting (null when unknown), `num_clusters` what the
clustering was asked for (-1 = pick a count by threshold), `found` how many voices
came back after the cap, `seconds` the worker's own time, and `resplit` (live reuse
only) how many live utterances that straddled a speaker change were re-transcribed
as pieces. Deepgram never writes it.

`mic_denoise` is written by the local provider (and by the live transcriber into
`.live.json`, from where it rides along when that file is reused as the transcript):
what noise reduction actually ran on the mic copy. `mode` is the setting at the time;
`applied` whether a filter ran; `filter` is `arnndn` (RNNoise) or `afftdn` (the
fallback), or null; `noise_floor_db` / `speech_level_db` are the measured 10th / 90th
percentile frame levels of the mic copy in dBFS (null when nothing was measured, e.g.
`mode: off`); `threshold_db` is `mic_noise_floor_db` as clamped; an `error` key
appears only when both filters failed and the raw copy was used. The `deepgram`
provider never writes the block. The block describes the temp copy the transcriber
heard; `audio.opus` is never modified.

## Mic noise reduction

`spitball/denoise.py` (docs/SPEC-v2.md section 3). Two `config.json` keys:

| key | default | meaning |
|:--|:--|:--|
| `mic_denoise` | `"auto"` | `"off"`, `"auto"`, or `"on"`; anything else reads as `"auto"`. |
| `mic_noise_floor_db` | `-45` | dBFS; `"auto"` denoises when the measured mic floor is above this. Clamped to −80…−20. |

Where it runs, in both cases on a TEMPORARY copy of the mic channel (channel 0) only:

- **Post-call (local provider):** `split_stereo_to_mono_wavs` writes the mic copy
  with `highpass=f=80` on its branch (the far copy gets no filter), then
  `denoise.prepare_mic` measures it (`audio.measure_levels`: `astats` RMS per 50 ms
  frame, `-inf` read as −100), takes the 10th percentile as the floor, and, when the
  mode says so, writes `channel-0-denoised.wav` and transcribes that instead.
- **Live:** `extract_channel_clip` applies the same `highpass=f=80` to channel-0
  tails; `LiveTranscriber._denoise_tail` feeds each tail's frame levels to a
  `denoise.LiveGate` (a rolling 60 s window; on when the floor is above the threshold,
  off only 3 dB below it) and denoises the tail into `live-tail-ch0-denoised.wav`
  before segmentation and transcription.

The filter is `apad=pad_dur=0.2,arnndn=m=<models/rnnoise/sh.rnnn>:mix=0.7`, output
cut back to the input's duration with `-t` and resampled to 16 kHz. The padding is
deliberate: ffmpeg 9's `arnndn` flushes its final partial frame against an
uninitialized buffer and leaves ~176 NaN samples (a full-scale click once written as
16-bit PCM) at the end of any clip whose length isn't a multiple of its 10 ms frame,
which is every live tail. When the model file is missing, or `arnndn` fails, the
chain is `afftdn=nr=12:nf=-40:tn=1` instead; if that fails too the raw copy is used
and the error recorded. `anlmdn` is never used (it aborts in ffmpeg 9.0.1). The model
path is escaped for the filtergraph (two levels), so an install path with `:` or `'`
in it still works.

On the Whisper path only (voxtype's engine is `whisper`), the local provider also
measures each channel's frame levels and uses them for speech detection: `silencedetect`'s gate becomes
`max(-35, floor + 10)` capped at −20 dB, and a speech window whose loudest frame is
within 6 dB of the floor is skipped as noise-only. Parakeet keeps the plain −35 dB
segmentation.

## Calendar events

`spitball/calendar.py` normalizes every source to one event shape. This is what
`.meta.json`'s `events`, `calendar test --json`'s `match`, and a `calendar_command`
all speak:

```json
{
  "id": "uid@google.com/2026-09-30T14:00:00-06:00",
  "uid": "uid@google.com",
  "title": "Weekly sync",
  "start": "2026-09-30T14:00:00-06:00",
  "end": "2026-09-30T14:30:00-06:00",
  "all_day": false,
  "status": "confirmed",
  "transparency": "opaque",
  "kind": "default",
  "my_response": "accepted",
  "organizer": {"name": "Alex Demo", "email": "alex@example.com"},
  "attendees": [{"name": "…", "email": "…", "response": "accepted", "self": false, "optional": false}],
  "conference": {"kind": "meet", "url": "https://meet.google.com/abc-defg-hij", "code": "abc-defg-hij"},
  "location": "",
  "description": "…",
  "recurring": true,
  "recurrence_id": "2026-09-30T14:00:00-06:00"
}
```

| field | meaning |
|:--|:--|
| `id` | stable per instance: the UID, plus `/<original instance start>` for an instance of a recurring series |
| `start`, `end` | ISO 8601 with a UTC offset (the event's own zone); a bare `YYYY-MM-DD` for all-day events |
| `all_day` | `true` for date-only events (never matched) |
| `status` | `confirmed` \| `tentative` \| `cancelled` (a canceled event is never matched) |
| `transparency` | `opaque` \| `transparent` (marked free; never matched) |
| `kind` | `default`, or one of the kinds that are never matched: `focus`, `out_of_office`, `working_location`, `birthday` -- from the title (Google's feed has no event-type field) and Outlook's busy status |
| `my_response` | your own reply: `accepted` \| `declined` \| `tentative` \| `needs_action` \| `""` (unknown). Declined is never matched. From the ATTENDEE line whose address is `calendar_my_email`, or, when that's empty, the address that clearly dominates the feed with no tie (see "Owner" below); the organizer counts as accepted. |
| `attendees` | every human invitee (rooms/resources dropped), each with `self` |
| `conference` | the meeting link, or `null`: `kind` `meet` \| `zoom` \| `teams` \| `webex`, and `code` -- the Meet code (`abc-defg-hij`) or Zoom meeting id -- from `X-GOOGLE-CONFERENCE`, LOCATION, URL, Teams' `X-MICROSOFT-SKYPETEAMSMEETINGURL`, or the description |
| `description` | capped at 4,000 characters |

**`calendar_command` contract.** With `calendar_source: "command"`, Spitball runs
`calendar_command` through the shell with `SPITBALL_WINDOW_START` /
`SPITBALL_WINDOW_END` (ISO 8601) in the environment and reads a JSON array of events
(or `{"events": [...]}`) from stdout. Each event needs at least `title` and `start`;
`end` defaults to an hour later. Times may be ISO 8601 (an offset is respected; naive
means local) or epoch seconds; a bare `YYYY-MM-DD` marks an all-day event.
`attendees` may be objects as above, `"Name <email>"` strings, or bare names;
`organizer` an object or a name; `conference` an object, a URL string, or omitted
(`hangoutLink`, `location`, and `description` are then searched for a link).
Google-API-style names are accepted too (`summary`, `displayName`,
`responseStatus`, `eventType`, `hangoutLink`). Events outside the window are
dropped. A non-zero exit, a timeout (20 s), or non-JSON output is reported as the
source's `error`; recording is never affected.

**Recurrence.** A series is expanded on the spot from its RRULE (in the event's own
zone; `EXDATE`, `RECURRENCE-ID` overrides, and `COUNT`/`UNTIL` honored). The
expander implements exactly: `FREQ` `DAILY` / `WEEKLY` / `MONTHLY` / `YEARLY`;
`INTERVAL`, `COUNT`, `UNTIL`, `WKST`; `BYDAY` (plain days for `DAILY`/`WEEKLY`,
ordinal days such as `-1WE` for `MONTHLY`/`YEARLY`); `BYMONTHDAY` and `BYMONTH` for
`MONTHLY`/`YEARLY` (both `BYDAY` and `BYMONTHDAY` given = their intersection, RFC
5545); `BYSETPOS` for `MONTHLY` (over each month's set) and `YEARLY` (over the
year's set across `BYMONTH`), e.g. `FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1`
is the last weekday of the month. **Any other part** (`BYWEEKNO`, `BYYEARDAY`,
`BYHOUR`/`BYMINUTE`/`BYSECOND`, `RSCALE`, an `X-` part, sub-daily `FREQ`, or a
supported part in a combination the expander doesn't handle -- `BYMONTHDAY` on a
`WEEKLY` rule, `BYSETPOS` without a set, `BYDAY` on a `YEARLY` rule with no
`BYMONTH`) makes the **whole series skipped**, never expanded approximately: a
made-up instance could confidently match a recording. Its moved overrides are
concrete events and still count. Skips are counted: `rules_skipped` in the
`.meta.json` snapshot and in `calendar test --json` (with a `Skipped:` line in the
plain report), so a feed that leans on such a rule is visible rather than silently
thin.

**Owner.** Which attendee is you (`self`, `my_response`): `calendar_my_email` when
set; otherwise the address that appears on the most invites in the feed, and only
when it appears on at least half of at least three invites **and no other address
ties it**. A tie (a feed that is mostly 1:1s with one person) is never broken by
guessing -- the owner stays unknown, nobody is `self`, `my_response` is `""`, and
`calendar test` reports `my_email_known: false` (the Calendar page then asks for
your address).

**Matching.** Candidates are events whose span runs from 15 minutes before the
recording started to 10 minutes after; hard filters drop all-day, canceled,
transparent, non-default `kind`, and declined events; the rest score: +100 for a Meet
code that matches a window title (−50 for a different one, which also forfeits the
host credit), +30 when the meeting link's host fits the app on the mic (+15 for a
Zoom/Teams/Webex link with a browser, −30 for a clear conflict such as a Meet link
with Zoom on the mic), +20 when the recording started during the event, +10 per other
non-declined invitee up to three, +10 accepted / +5 no reply, −1.5 per minute the
start is more than 5 minutes from the event's start (capped at −30), and, once the
duration is known, up to +20 for the share of the recording that fell inside the
event. The best candidate must score at least 40 and beat the runner-up by 15 or
there is no match (`confident: false`); `confidence` is the best score either way.

## Settings overlay

Settings is not a bar dropdown: `SettingsWindow.qml` is a full-screen transparent
layer-shell window on the Overlay layer (exclusive keyboard focus while open) with a
scrim and a centered card, the same surface Omarchy's menu/emoji/clipboard pickers
use. The card (`settings/SettingsCard.qml`) has a pinned header (title, daemon state
line, ✕), a section nav on the left (`Model.settingsSections()`: General, Recording,
Transcription, Live, Audio, Speakers, Summary, Calendar, Storage, About), and one
page on the right. Esc, an outside click, or ✕ closes it. With no control focused,
`j`/`k`/Up/Down move between sections, `1`-`9` jump, Tab walks the page's controls.

The widget's `openSettings(section)` is the only entry point (the menu's Settings…
item passes `general`, every setup prompt passes `transcription`). IPC target
`supercleanse.spitball-settings`: `open`, `openSection <id>`, `close`, `toggle`.

Every value shown comes from `spitball config get --json`; every change goes through
`config set` / `config set-secret` (stdin) / `config unset` immediately, and the
other read-only commands in the CLI table (`local info/models`, `live status`,
`diarize status`, `calendar test`,
`check`, `status`) fill the pages. `settings/SettingsStore.qml` owns all of those
round trips; pages never spawn processes. Every key in `config.json` has a control.

**Layering rule.** Before the store launches anything that opens an ordinary window
-- `pick-folder`, the voxtype installer, `live setup` or `diarize setup` in a
terminal, or `local set-model` (which may show a pkexec prompt) -- it asks the widget to close the
overlay, because a layer-shell surface with exclusive keyboard focus sits above every
normal window. `pick-folder` reopens it when the picker resolves (any exit code);
installers and model switches leave it closed, and their progress is watched on the
bar widget / `model.json` until the user reopens Settings.

## Live popup

A bar dropdown popup (the same `KeyboardPanel` component as the menu),
opened by the menu's Start recording / Show live transcript items and closable
with its header ✕, Esc, or an outside click without affecting the recording. Header:
app name, red dot, elapsed time (from `started_at`), a **Stop recording** button (the
only way to stop from this popup), and the ✕. Body: a chat-style transcript fed by `live.json` below -- my lines
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
"note": "live transcript", "mic_denoise": {...}}` -- the last block is what noise
reduction ran on the mic tails, see "Transcript cache") -- see `spitball/process.py`'s `process()`: if
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
