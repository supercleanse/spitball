# Spec: Spitball v2

Decisions from the 2026-09-30 design interview. Four features, built in this order on
one branch (`spitball-v2`), each as its own commit or small set of commits, with one
pull request at the end.

1. Settings overlay (this document, section 1) — shipped in phase 1.
2. Calendar (section 2) — phase 2.
3. Noise (section 3) — phase 3.
4. Speakers (section 4) — phase 4.

## Build rules

- Match the existing code: a standard-library-only Python core, an optional venv for
  heavy extras (the `spitball live setup` pattern), the CLI contract in `CONTRACT.md`,
  tests under `tests/` (kept green; everything new gets tests). Update `README.md`,
  `CONTRACT.md`, and `docs/ROADMAP.md` as features land. American English spelling.
- Never put a window on the developer's live desktop while building: no shell reloads,
  no opening panels, no screenshots of the real screen, no test notifications, no
  restarting a running daemon. Validate with `qmllint`, the unit tests, and offscreen
  rendering (`tests/offscreen/render.sh`, which runs Quickshell with
  `QT_QPA_PLATFORM=offscreen` against a fake CLI). Fake data lives only behind that
  explicit harness and never reaches the real plugin.

## 1. Settings overlay

A centered layer-shell overlay in the style of Omarchy's own menu, emoji, and clipboard
surfaces replaces the old bar-anchored dropdown (`SettingsPanel.qml`, removed).

- **Surface.** `SettingsWindow.qml` is a full-screen transparent `PanelWindow` on the
  Overlay layer with exclusive keyboard focus while open: a scrim (`Color.menu.scrim`),
  a centered `BorderSurface` card (`Color.popups.*`, `Border.surfaceSpec("popups", …)`),
  Esc / click-outside / ✕ to dismiss. Card size is `Style.space(680) × Style.space(480)`,
  capped to the screen minus `Style.gapsOut`.
- **Navigation.** A left column of sections (`settings/SettingsNav.qml`) and a per-page
  `Loader` on the right (`settings/SettingsCard.qml`). Sections, in order: General,
  Recording, Transcription, Live, Audio, Speakers, Summary, Calendar, Storage, About.
  The list lives in `Model.js` (`settingsSections()`) so it is unit-tested and so later
  phases add a page by adding one entry plus one QML file.
- **Keys.** When no control has focus: `j`/`k`/Up/Down move between sections, `1`–`9`
  jump, Tab walks the page's controls, Esc closes. When a text field, number field, or
  dropdown has focus, keys go to it; Esc returns focus to the section list first.
- **Theme.** Everything reads Omarchy tokens (`Color.popups.*`, `Color.menu.scrim`,
  `Style.space`, `Style.font.*`, `Style.selectedFillFor` / `hoverFillFor`) so a theme
  switch restyles the overlay live.
- **Plumbing.** `settings/SettingsStore.qml` holds every CLI round trip the old panel
  had (`config get/set/set-secret/unset`, `check`, `local info/models/set-model`,
  `live status/setup`, `pick-folder`, `status`) plus the model-switch watch on
  `model.json`. Pages are thin views over the store. Row components (`FieldRow`,
  `SecretRow`, `ToggleRow`, `NumberRow`, `TestRow`, `FolderRow`, `ErrorNote`,
  `NoteText`, `RevealRow`, `SettingsPage`) are shared files under `settings/`.
- **Layering rule (unchanged).** The overlay closes itself (`requestClose`) before
  anything that opens an ordinary window — the portal folder picker, the voxtype
  installer, the live-engine installer, or a model switch's `pkexec` prompt — because a
  layer-shell surface with exclusive keyboard focus would sit above them. Synchronous
  pickers reopen it (`requestReopen`) when they resolve; long-running installers and
  model switches do not.
- **Entry points.** `openSettings(section)` in `Widget.qml` is the only way in: the
  menu's Settings… item opens General, the first-run "Set up transcription…" item and
  every other setup prompt open Transcription. The IPC target
  `supercleanse.spitball-settings` keeps `open`/`close`/`toggle` and gains
  `openSection <id>`.
- **Every config key has a control.** The eleven keys the old panel never exposed now
  live on their pages: `call_apps`, `detect_after_s`, `end_after_s`, `min_call_s`,
  `min_manual_s`, `opus_bitrate` (Recording); `live_transcript`, `live_engine`,
  `live_max_window_s` (Live); `deepgram_api_key_command` (Transcription, under
  Advanced); `summary_api_key_command` (Summary, under Advanced).
- **Placeholders.** Audio, Speakers, and Calendar ship as pages that say "Coming in
  this release" plus whatever read-only context is already available, so phases 2–4
  only add controls.

## 2. Calendar — secret iCal/ICS URL + `calendar_command` hook

- Built-in source: Google's "Secret address in iCal format" or any ICS/webcal URL,
  stored as a secret (`calendar_ics_url`, with a `calendar_ics_url_command` variant like
  the other secrets). Standard library only; a bounded RRULE expander.
- Escape hatch: `calendar_command`, a shell command whose stdout is normalized JSON
  events (document the shape in `CONTRACT.md`).
- No Google OAuth, no shipped client ID. No CalDAV for now.
- Matcher: time overlap, hard filters (all-day, cancelled, transparent, declined,
  focus/out-of-office), then scoring; a Google Meet code from the window title is the
  exact key; a confidence threshold; candidates snapshot into `.meta.json` at record
  start. Calendar failures never block recording.
- A match is used for all of: naming the call folder (confident matches only; never
  rename on a weak match), the meeting header and attendees at the top of the
  transcript, attendee names and description fed to the summarizer, and speaker
  labeling.
- The developer's real feed reaches the build only through
  `calendar_ics_url_command` = `cat ~/.config/spitball/calendar-ics-url` once that file
  exists. It is a bearer credential: never print, log, or commit it or any event data
  from it. Report only structural findings ("ATTENDEE lines present: yes").

## 3. Noise — free wins plus optional denoise

- Free wins: `highpass=f=80` on the mic split; better voice-activity detection on the
  Whisper path where it applies; a README note on PipeWire `module-echo-cancel` and
  EasyEffects.
- `mic_denoise: off | auto | on` (default `auto`): ffmpeg `arnndn` with a vendored
  RNNoise model (`mix=0.7`), `afftdn` as the fallback, applied to a temporary copy of
  the mic channel only, on both the post-call and live paths. `auto` is gated on the
  measured mic noise floor. The raw `audio.opus` is never touched. Do not use `anlmdn`
  (it crashes ffmpeg 9.0.1). No neural add-on, no Spitball-owned PipeWire filter.
- Settings: the Audio page.

## 4. Speakers — naming plus a local split

- Naming: an LLM pass maps speaker labels to calendar attendees using vocatives and
  introductions, with a confidence; an uncertain match reads "Speaker 2 (probably X)".
  One-to-one calls map "Them" to the single other invitee. A `speakers` map persists in
  `.transcript.json`; `spitball speakers <dir> [n "Name"]` lists and corrects, then
  re-renders the transcript, summary, and export.
- Local split: sherpa-onnx diarization of the far channel only, inside the optional
  live-engine venv (models from k2-fsa GitHub releases, no Hugging Face token). Skipped
  when exactly one remote attendee is expected; `num_clusters = attendees − 1` when
  known. The Deepgram path is unchanged.
- No pitch or gender inference. No voiceprint memory.
- Settings: the Speakers page.
