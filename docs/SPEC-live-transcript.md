# Spec: live transcript popup

## Why
A user clicked the red recording dot expecting a menu, and it stopped the
recording mid-call. Clicking the dot while recording must never stop it.

## Behavior

**Bar widget clicks** (superseded 2026-09-29: any click now opens the menu, which
holds Start, Show live transcript, and Stop -- see CONTRACT.md's bar widget section)
- `recording`: left click opens the **Live popup** (a bar dropdown popup, same
  component as the settings popup and Omarchy's audio/network popups). It no
  longer stops the recording.
- `detected`: left click starts recording (unchanged) and opens the Live popup.
- Right-click menu unchanged (still has Stop, Settings…, etc.).

**Live popup**
- Header: app name, red dot, elapsed time (from `started_at`), and a clear
  **Stop recording** button (the only way to stop from this popup; one click, no
  confirm).
- Body: chat-style transcript. My lines (channel 0) are bubbles on the right
  (accent color); theirs (channel 1) on the left (neutral). Speaker label above a
  bubble only when the speaker changes. Timestamp in small text (m:ss).
- Scrollable. Auto-scroll to the newest bubble only when the view is already at
  the bottom; if the user scrolled up, keep their position and show a small
  "New messages ↓" pill that jumps to the bottom.
- Status line at the bottom: "Listening…" while live transcription is keeping up,
  or the reason it isn't available (e.g. "Live transcript needs voxtype").
- When recording stops (state leaves `recording`), the popup shows "Recording
  saved; transcribing…" and a "Close" button; it may auto-close after a few
  seconds.
- Popup can be closed with Esc / outside click without affecting the recording.

**Settings**: the auto-record control ("Record automatically when a call
starts") moves to the top of the Recording section and gets one line of help:
"Every detected call is recorded without asking. Tell people you're recording."

## Live transcription (daemon)

- New setting `live_transcript` (default `true`). Runs only while recording and
  only if the local provider is available (voxtype installed). It uses the
  local provider **regardless of `transcription_provider`**, so it never costs
  money or sends audio anywhere during the call.
- A live-transcriber thread started with each recording. Every ~2 s it looks at
  the growing `audio.opus` (Ogg is readable while ffmpeg is still writing it;
  decode with ffmpeg up to the current end — verify this works and handle a
  truncated last page), splits it into the two channels, and finds speech
  segments **closed by a pause** (a segment is only transcribed once a
  silence ≥0.5 s follows it, or once it reaches `live_max_window_s` = 12 s,
  then it's cut). Transcribe each closed segment with the same
  `_transcribe_window` logic the local provider uses (retry/halving, failure
  marker, log-line stripping). Track per-channel progress so each stretch of
  audio is transcribed once.
- Apply the same echo removal as `build_transcript` incrementally (drop a mic
  segment that overlaps a far-side one in time and matches it ≥0.6).
- Publish to `$XDG_RUNTIME_DIR/spitball/live.json` (atomic writes):
  `{"call_id": "<call folder name>", "started_at": <epoch>, "status":
  "listening"|"catching-up"|"unavailable"|"stopped", "message": "",
  "utterances": [{"channel", "start", "end", "transcript", "failed"?}]}`
  sorted by start. `catching-up` when more than ~20 s of closed audio is waiting.
  Keep the file after the recording stops (status `stopped`) until the next
  recording starts.
- CPU: one voxtype process at a time; never more than one live thread.
- On stop: the live thread finishes the remaining audio (with a time limit),
  then writes its utterances into the call folder as `.live.json`.
  **If `transcription_provider` is `local`**, `process()` uses `.live.json` as the
  transcript (normalized shape, `provider: "local"`, note "live transcript")
  instead of transcribing again — unless it's missing, empty, or has failures
  covering more than 10% of the call, in which case run the normal full
  transcription. Deepgram provider: always transcribe the full file as today.
- `reprocess --retranscribe` ignores `.live.json`.

## Contract
Update CONTRACT.md: new clicks, `live.json`, `.live.json`, `live_transcript`
setting, `live_max_window_s`.

## Tests
- Offline: incremental segmentation on a synthetic growing file (write a test
  WAV/Ogg in stages), closed-vs-open segment logic, each audio span transcribed
  once, echo removal incremental, catching-up status, stop flushes remaining
  audio, `.live.json` reuse rules in `process()` (used / fallback cases),
  live.json atomicity and schema, thread lifecycle (started with recording,
  stopped with it, never two at once). Mock voxtype.
- Model.js: bubble grouping (speaker change shows label), side by channel,
  timestamp format, auto-scroll decision helper, status text per status.
- Live (gated): a real 20 s recording with the fake-Zoom stream while playing
  nothing — just verify the thread runs, live.json is well-formed, and the
  recording's own file is still valid afterward. Plus: feed the cached
  two-channel sample through the live pipeline as a simulated growing file and
  check both channels appear in order.
- Isolation rules in `tests/__init__.py` apply.
