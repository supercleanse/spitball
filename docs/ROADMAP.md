# Spitball roadmap

## Phase 1 (shipped)

Recording, detection, the two transcription providers `local` (voxtype) and
`deepgram`, summaries, the settings/config CLI, `setup_needed`, and
`pick-folder`. See `docs/SPEC-settings-and-providers.md` §§1-3, 7 (local),
and 8 (the phase cut itself).

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
