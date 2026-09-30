# Phase 2 UI drafts (OpenAI-compatible / AssemblyAI / Soniox)

Per `docs/SPEC-settings-and-providers.md` section 8 ("phase 1 = voxtype +
Deepgram only", which narrows section 7's five-provider design), phase 1
ships exactly two transcription providers in the Settings panel: Local
(voxtype) and Deepgram. OpenAI-compatible, AssemblyAI and Soniox move to
phase 2, once the user has keys to test them live.

## What's here

This directory holds both halves of the deferred work: `providers-openai-
assemblyai-soniox.patch` is the backend's draft (new `spitball/multipart.py`
+ `spitball/providers/openai.py` etc.), written in parallel by the backend
side of this same build. The two UI-side patches below are this file's own
concern:

- `settings-ui-openai-assemblyai-soniox.patch` -- adds back the
  **OpenAI-compatible** section of `SettingsPanel.qml` (masked API key,
  model field, an "Advanced" reveal for a custom base URL, and a Test
  button), plus the small bits of state it needs (`openaiTesting`,
  `openaiTestMsg`, `openaiAdvancedOpen`) and a `testTranscription()` branch.
  Apply with `patch -p1 < docs/phase2/settings-ui-openai-assemblyai-soniox.patch`
  from the repo root, or just read it as a reference and re-type the block.
- `model-js-provider-list.patch` -- adds `assemblyai`, `soniox`, and
  `openai` back into `Model.js`'s `TRANSCRIPTION_PROVIDERS` list (and
  therefore the three-... now five-way `ButtonGroup` in the Settings
  panel).

**AssemblyAI and Soniox have no UI patch** -- the UI work reached the
five-provider design in `docs/SPEC-settings-and-providers.md` section 7
only as a plan; by the time actual QML got written, the very next amendment
(section 8) narrowed scope to phase 1 before an AssemblyAI or Soniox
section was ever built. Their sections should mirror Deepgram's exactly
(masked key with a source placeholder, a model text field, a Test button) --
copy the Deepgram `Column` in `SettingsPanel.qml` and its `SecretRow`/
`FieldRow`/`TestRow` trio, swapping in `assemblyai_api_key`/`assemblyai_model`
or `soniox_api_key`/`soniox_model`.

## Test status

**Never exercised against a live backend or in the running shell.** The
OpenAI section was written and briefly ran live inside the plugin (visually
verified once, screenshot not kept) before section 8 landed and it was
pulled back out; it was not covered by any `tests/js/model.test.js` case of
its own (nothing OpenAI-specific ever went into `Model.js` beyond the
provider-list entry -- the masked-key/model-field/test-button pattern is
the same generic `SecretRow`/`FieldRow`/`TestRow` components the Deepgram
section uses, which *are* covered and *are* live-verified in phase 1).
Treat this patch as a faithful starting point, not tested phase-2-ready
code:

- It targets `SettingsPanel.qml` as of this phase-1 handoff. If the file
  has moved on (e.g. `testTranscription()`'s single deepgram-shaped
  testing/message pair gets generalized to a provider-keyed map, which the
  patch's own comments suggest doing anyway once there's more than one
  non-local provider to test), the patch will need manual reconciliation,
  not a blind `patch -p1`.
- The masked-key `SecretRow` for a fourth+ provider (once AssemblyAI/Soniox
  land) will make `testTranscription()`'s per-provider boolean-pair pattern
  unwieldy; switch to a `{provider: {testing, message}}` map at that point
  rather than adding a fourth pair of properties.

## What phase 2 needs from the backend first

Per section 7 of the spec: AssemblyAI and Soniox are net-new providers
(`ASSEMBLYAI_API_KEY`/`SONIOX_API_KEY`, upload → create → poll → map-to-
utterances flows, each with its own `check transcription` branch). OpenAI-
compatible already had a working backend design in section 1/7 before being
deferred. None of the three has settings keys in `config get --json` yet in
phase 1's backend. See `docs/ROADMAP.md` for the full phase 2 list.
