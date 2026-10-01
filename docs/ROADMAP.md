# Spitball roadmap

## Phase 1 (shipped)

Recording, detection, the two transcription providers `local` (voxtype) and
`deepgram`, summaries, the settings/config CLI, `setup_needed`, and
`pick-folder`. See `docs/SPEC-settings-and-providers.md` §§1-3, 7 (local),
and 8 (the phase cut itself).

## v2 (in progress on `spitball-v2`)

Four features, in order, per `docs/SPEC-v2.md`:

1. **Settings overlay** (done) -- the centered layer-shell settings window with a
   section nav, replacing the bar dropdown; every config key has a control.
2. **Calendar** (done) -- match calls to events from a secret ICS feed or a
   `calendar_command` (`spitball/calendar.py`: stdlib ICS parser + bounded
   RRULE expander, cached feed, deterministic matcher keyed on the Meet code
   from window titles); names the folder, heads the transcript, feeds the
   summarizer, stores `meeting.attendees` in `.transcript.json` for phase 4;
   `spitball calendar test`; fills the Calendar page. Not done, by design:
   Google OAuth (no shipped client ID), CalDAV, an `event_title` field in
   `state.json` for the bar/Live popup.
3. **Noise** (done) -- `highpass=f=80` on the mic copy (split and live tails),
   `mic_denoise: off|auto|on` (`spitball/denoise.py`: RNNoise via ffmpeg `arnndn`
   with the model vendored in `models/rnnoise/`, `mix=0.7`, `afftdn` fallback, a
   temp copy of the mic channel only, on both the post-call and live paths, `auto`
   gated on the measured noise floor), the `mic_denoise` block in
   `.transcript.json`/`.live.json`, a stricter speech check on the Whisper path
   (adaptive `silencedetect` gate + noise-only windows skipped), the README's
   PipeWire echo-cancel / EasyEffects notes; fills the Audio page. Not done, by
   design: Silero VAD (needs onnxruntime + a 2 MB model, i.e. the optional venv),
   GTCRN/DeepFilterNet (research option C), a Spitball-owned PipeWire filter
   (option D), denoising for the Deepgram upload.
4. **Speakers** (done) -- `spitball/speakers.py`: far-side ids folded into
   labels (tiny voices and voices beyond `speaker_max` fold into their
   neighbors), a `speakers` map in `.transcript.json` (label → name, confidence,
   source, evidence, provider id), a 1:1 named from the invite with no model
   call, otherwise one naming call to the summary endpoint with the neutral
   transcript and the invite list (strict JSON, names off the invite rejected,
   duplicates made unsure), rendered as the name / `Speaker 2 (probably X)` /
   the bare label; `**Speakers:**` and invite-vs-voices mismatch header lines;
   `spitball speakers <dir> [n "Name" | --clear]` with a re-render (summary
   wording rewritten, no new model call; `reprocess` keeps user names).
   `spitball/diarize.py` + `diarize_worker.py`: sherpa-onnx offline diarization
   of the far channel only (pyannote segmentation-3.0 int8 + 3D-Speaker ERes2Net,
   from the k2-fsa GitHub releases, SHA-256 pinned, no Hugging Face token),
   installed by `spitball diarize setup` into the live-engine venv; skipped for
   a 1:1, `num_clusters` from the invite count capped at `speaker_max`, else
   threshold clustering; voxtype windows cut at speaker changes before
   transcription; a reused live transcript is labeled by overlap and its
   straddling lines re-transcribed as pieces; any failure keeps one "Them" with
   a `diarization` block saying why. Fills the Speakers page. Not done, by
   design: pitch/gender inference (never), voiceprint memory across calls
   (biometric; would need opt-in + a delete command), a rename UI in the popup
   (the CLI is enough for now), a user-facing clustering-threshold knob
   (constant 0.7), a speaker-count hint for Deepgram (its API takes none), live
   (mid-call) speaker splitting in the popup.
5. **Meeting reminders** (done) -- `spitball/reminders.py`: a minute before
   (`calendar_remind_before_s`) any non-declined event with a Zoom / Meet /
   Teams / Webex link, a Spitball notification with **Join & record** (opens
   the link, starts a recording pinned to that event via
   `calendar.override` + `calendar.pinned`, follows the call once an app takes
   the mic) and **Dismiss**; a strict https + host allowlist for the link,
   `xdg-open` without a shell; once per occurrence across restarts
   (`reminded.json`), nothing for meetings more than two minutes old or while
   recording; plain-toast fallback when the server has no action support;
   `spitball calendar upcoming`; the two controls on the Calendar page. Not
   done, by design: `zoommtg://` / native-client links (the browser link is
   the one every invite carries), a sticky (critical-urgency) toast, a
   per-event snooze, `event_title` in `state.json`.

## Phase 2: more transcription providers

Parked per spec amendment §8 so each can get a real API key and a live test
before it ships, rather than landing untested. The provider abstraction
(normalized `{provider, model, utterances}`, `.transcript.json` caching,
per-provider `check()`/`ready()`) is already built to take these additively --
adding one is: a new `spitball/providers/<name>.py`, an entry in
`providers._PROVIDERS`, its settings keys in `config.DEFAULTS` (and
`SECRET_KEYS`/`SECRET_ENV` if it takes a key), and its offline + live tests.

- **OpenAI-compatible** -- works against the real OpenAI API or any
  compatible server (Groq, speaches/faster-whisper-server, LocalAI, a
  whisper.cpp server). Already drafted (unwired, untested) -- see
  `docs/phase2/providers-openai-assemblyai-soniox.patch` and its README.
  The Settings panel's OpenAI-compatible section is likewise drafted at
  `docs/phase2/settings-ui-openai-assemblyai-soniox.patch` (+
  `docs/phase2/model-js-provider-list.patch` for the provider list) -- see
  `docs/phase2/README.md` for both halves.
  Worth also covering `gpt-4o-transcribe-diarize`'s native speaker-label
  output when it lands, instead of only the segment-per-chunk fallback.
- **AssemblyAI** -- async transcription API. Upload (`POST
  https://api.assemblyai.com/v2/upload`) then create a transcript (`POST
  /v2/transcript`) with `multichannel: true` **and** `speaker_labels: true`
  (both are required together for utterances to carry a `channel` field),
  poll `GET /v2/transcript/{id}` until `status` is `completed`/`error`.
  Gotchas found during phase-1 research, re-verify before coding: channel is
  1-indexed (1/2, not 0/1) and comes back as a **string**; timestamps are
  milliseconds; the current `speech_model` field/value naming was unclear as
  of this research (older `best`/`nano`/`universal` names vs. newer
  `universal-2`/`universal-3-5-pro` -- confirm against
  `https://assemblyai.com/docs/llms.txt` before picking a default).
- **Soniox** -- async transcription API, no confirmed request shape yet
  (docs fetched during research were overview pages, not the API reference).
  No native multichannel was mentioned in what we found -- if that holds,
  split the stereo file the same way the openai/local providers do and
  diarize the far channel only. Delete the uploaded file/transcription
  afterward if the API supports it (privacy -- audio shouldn't linger on a
  third party's servers past the call it was needed for).

Each phase-2 provider needs, before merge:
- Offline unit tests against a local `http.server` fake (upload, create,
  poll including an error status, and utterance mapping).
- A live test gated on that provider's own API key env var
  (`OPENAI_API_KEY` / `ASSEMBLYAI_API_KEY` / `SONIOX_API_KEY`), skipped when
  unset -- never required for `tests/run.sh --live` to pass.
- Its three settings keys added to the Settings panel's Transcription
  section (provider choice grows from two to N) and to `config get`'s
  output.
