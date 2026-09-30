# Spec: transcription providers + settings panel

Two workstreams build against this spec in parallel. The **backend** owns
everything under `spitball/` and the new CLI commands. The **UI** owns the QML
(`Widget.qml`, new `SettingsPanel.qml`, `Model.js`). They meet only at the CLI
and the state file defined here.

## 1. Transcription providers

Setting `transcription_provider`: `"deepgram"` (default) | `"openai"` | `"local"`.

Every provider returns the same normalized structure so `build_transcript()`
and echo removal work unchanged:

```json
{"provider": "openai", "model": "whisper-1",
 "utterances": [{"channel": 0, "speaker": 0, "start": 1.2, "end": 4.8, "transcript": "text"}]}
```

Channel 0 = my mic, channel 1 = the far side. Cache it as `.transcript.json` in the
call folder (reprocess reuses it; keep reading an old `.deepgram.json` for
folders made before this change). `spitball reprocess <dir> --retranscribe`
ignores the cache.

### deepgram (existing)
Key: `DEEPGRAM_API_KEY` env → `deepgram_api_key` → `deepgram_api_key_command`.
Model setting `deepgram_model` (default `nova-3`). Multichannel + diarize, so the
far side can have several speakers.

### openai
Works with OpenAI and any OpenAI-compatible transcription server (Groq,
speaches/faster-whisper-server, LocalAI, a whisper.cpp server…).
- `openai_base_url` (default `https://api.openai.com/v1`), `openai_model`
  (default `whisper-1`), key `OPENAI_API_KEY` env → `openai_api_key` →
  `openai_api_key_command` (key optional when base_url is not api.openai.com).
- No multichannel support, so split the stereo file into two mono 16 kHz files
  with ffmpeg and transcribe each (`POST {base}/audio/transcriptions`,
  multipart form built with stdlib, `response_format=verbose_json`,
  `timestamp_granularities[]=segment`, `language=en`). Map each segment to an
  utterance on that channel (speaker 0).
- Upload limit is 25 MB: encode the mono parts as Opus/Ogg ~24 kbps (≈11 MB/hour)
  and, if a part would still exceed 24 MB, split it into ≤20-minute chunks and
  offset their timestamps.
- If a model returns no segments (e.g. `gpt-4o-transcribe` only returns text),
  fall back to one utterance per chunk with the chunk's start/end.

### local (voxtype, ships with Omarchy)
`voxtype transcribe <file>` (WAV, 16 kHz, mono) prints plain text on stdout
after some header lines (strip lines starting `Loading audio file:`,
`Audio format:`, `Processing `; logs go to stderr). No timestamps.
- Split each channel into speech segments with ffmpeg `silencedetect`
  (noise −35 dB, ≥0.6 s), merge neighbors into windows of at most ~30 s, and
  transcribe each window: `voxtype -q --model <local_model> transcribe win.wav`.
  Each window → one utterance with its start/end. Skip windows under 0.4 s.
- `local_model` default `base.en`. Models live in voxtype's cache,
  `~/.local/share/voxtype/models/ggml-<name>.bin`. Offer: `base.en`,
  `small.en`, `medium.en`, `large-v3-turbo`. Download missing ones ourselves from
  `https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-<name>.bin`
  (stream to `.part`, rename when complete) — never run `voxtype setup`, which
  can change the user's dictation config.
- If `voxtype` is not installed, the provider reports "not available" with a hint.

## 2. Settings: config CLI

`~/.config/spitball/config.json` stays the single source of truth; the panel
only uses these commands. Writes are atomic, and the file is written **mode
600** once any key is stored in it. After any `set`, the daemon reloads config
(send `reload` over the socket; ignore if the daemon is down).

| command | output |
|:--|:--|
| `spitball config get --json` | JSON of the effective settings (defaults merged). Secret values are never printed: each `*_api_key` becomes `{"set": true, "source": "config"\|"env"\|"command"\|"none"}`. |
| `spitball config set <key> <value>` | Sets one key (JSON-typed: `true`/`false`/numbers parsed; strings as-is). Unknown keys rejected. Exit 0 / nonzero + message. |
| `spitball config set-secret <key>` | Reads the value from **stdin** (never argv) for `deepgram_api_key` / `openai_api_key` / `summary_api_key`. Empty stdin clears it. |
| `spitball config unset <key>` | Back to the default. |
| `spitball check transcription [--provider P] --json` | `{"ok": bool, "message": "…"}`. deepgram: GET `/v1/projects` with the key. openai: GET `{base}/models` (skip when no key and non-OpenAI base → try anyway). local: voxtype on PATH and model file present. |
| `spitball check summary --json` | `{"ok": bool, "message": "…", "models": [...]}` from `{summary_base_url}/models`. |
| `spitball local models --json` | `[{"name": "small.en", "size_mb": 466, "installed": true}]` for the four offered models. |
| `spitball local download <name>` | Downloads in the background (detached); progress in `$XDG_RUNTIME_DIR/spitball/download.json` = `{"name", "done_bytes", "total_bytes", "state": "downloading"\|"done"\|"error", "message"}`. |
| `spitball pick-folder [--title T]` | Opens a native folder chooser and prints the chosen absolute path (exit 1 if canceled). Use the xdg-desktop-portal FileChooser (`org.freedesktop.portal.FileChooser.OpenFile` with `directory=true`) via `gdbus`, waiting for the `Response` signal with `gdbus monitor`; fall back to `zenity --file-selection --directory` or `kdialog` if present; else exit 2 so the UI shows a text field. |

Settings keys the panel edits (all exist in DEFAULTS):
`calls_dir`, `export_dir` (empty = off), `auto_record` (moves from persist.json
into config; migrate the old value once), `my_name`, `transcription_provider`,
`deepgram_model`, `deepgram_api_key(_command)`, `openai_base_url`,
`openai_model`, `openai_api_key(_command)`, `local_model`, `summary_enabled`,
`summary_base_url`, `summary_model`, `summary_api_key(_command)`.
`spitball auto on|off|toggle` keeps working and writes `auto_record`.

## 3. State file additions (CONTRACT.md)

Add one field to `state.json`: `"setup_needed": ""` — empty when ready, else a
short reason such as `"Add a Deepgram key"`, `"Download a local model"`,
`"voxtype not installed"`. Computed at daemon start and after each reload
(cheap checks only: key present / model file present / voxtype on PATH — no
network). Detection and recording still work while setup is needed;
processing will fail with the same reason.

## 4. Settings panel (UI)

Right-click menu gets **Settings…**. It opens a popup panel styled like
Omarchy's own panels (look at `/usr/share/omarchy/shell/plugins/panels/*` and
`Ui` components; match OmaStats' settings page feel,
`~/.config/omarchy/plugins/crmne.omastats/`). Load values with
`spitball config get --json` on open; save each control on change with
`config set` (secrets via `set-secret` over stdin). Sections:

1. **Recording** — Save calls to: path + *Change…* (`pick-folder`; if it exits 2,
   make the path editable) + *Open*. Record automatically when a call starts
   (toggle). Your name (text).
2. **Transcription** — three-way choice Deepgram / OpenAI Whisper / Local.
   - Deepgram: API key (masked; placeholder shows "Set" / "From command" /
     "Not set" per `source`), model (text, default nova-3), *Test* button →
     `check transcription --json`, shows ✓ or the error.
   - OpenAI: API key (masked), model (text, default whisper-1), *Advanced* reveal
     with base URL, *Test*.
   - Local: model dropdown from `local models --json` with size + installed mark,
     *Download* for missing models showing progress from `download.json`,
     note "Runs on this computer with voxtype. Slower than cloud; audio never
     leaves your machine."
3. **Summaries** — toggle; endpoint URL; model dropdown filled from
   `check summary --json` (with a text fallback); *Test*.
4. **Notes copy** — Also copy notes to: off/on + folder (*Change…*). "Audio is
   never copied."

Bar widget: when `setup_needed` is non-empty and state is idle, show the widget
(not collapsed) as a small gear/"Set up" glyph with the reason as tooltip; click
opens the Settings panel. Settings stays reachable from the right-click menu in
every state.

## 5. Tests

Backend: unit tests for each provider (mock HTTP with a local `http.server`
fake for OpenAI; mock subprocess for voxtype/ffmpeg segmenting), normalization,
chunking math, multipart encoding, config CLI (types, unknown keys, secrets never
printed, mode 600, reload sent, auto_record migration), check commands, local
models/download (fake HTTP server), pick-folder fallbacks (mock gdbus/zenity),
`setup_needed` computation. Live (gated): local voxtype on the two-channel test
file; OpenAI only if a key resolves (skip otherwise). UI: Model.js helpers for
the new states (`setup_needed`), node tests. All tests stay isolated per
`tests/__init__.py`.

## 6. Amendment: Parakeet is the default (added mid-build)

- `transcription_provider` default becomes **`"local"`**; new setting
  `local_engine`: `"parakeet"` (default) | `"whisper"`. `local_model` default
  depends on the engine: parakeet → `parakeet-tdt-0.6b-v3-int8`, whisper →
  `base.en`. Offered parakeet models: `parakeet-tdt-0.6b-v3-int8` (default),
  `parakeet-tdt-0.6b-v3`, `parakeet-tdt-0.6b-v2-int8` (English-only).
- Omarchy's voxtype package already installs ONNX builds that can run
  Parakeet, but the user's active voxtype binary is usually the Whisper one.
  Pick the binary per engine: whisper → `voxtype` on PATH; parakeet → the first
  of `/usr/lib/voxtype/voxtype-onnx-{cuda,migraphx,avx512,avx2}` that runs on
  this machine (`voxtype info variants` lists which are usable; parse it, or try
  `--version` on each and prefer avx2 when there is no GPU). Invoke:
  `<onnx-binary> -q --engine parakeet --model <local_model> transcribe win.wav`.
  Verify the exact flags against `voxtype transcribe --help` / `voxtype --help`.
  Never change the user's voxtype config or active variant.
- Model download for parakeet: find where voxtype stores parakeet models and
  their source URLs (`voxtype info models`, voxtype docs/source, the models
  cache dir). Preferred: run voxtype's own downloader against a **throwaway
  config** so the user's dictation config can't change (e.g.
  `voxtype -c <tmp>/config.toml setup --download --model <name>` if that only
  downloads; verify first in a temp HOME if unsure). Otherwise download the
  files directly the way the spec's whisper download works. Report real sizes in
  `local models --json` (HEAD request / known sizes).
- **Fallback:** if the selected local model isn't installed yet but whisper
  `base.en` (or any installed whisper model) is, transcribe with that and note it
  in the transcript header ("Transcribed with Whisper base.en while Parakeet
  downloads"). Only fail when no local model at all is installed.
- `setup_needed` for a fresh install with provider=local and no parakeet model:
  `"Download Parakeet (≈N MB) for local transcription"` — shown even when the
  whisper fallback works, but processing still succeeds via the fallback.
- UI: the Local section shows an engine choice (Parakeet, recommended /
  Whisper), the model dropdown for that engine, and the Download button with
  progress. The first-run gear opens Settings scrolled to Transcription.
- Never auto-download without a click.

## 7. Amendment: follow voxtype, add AssemblyAI + Soniox (SUPERSEDES §6)

§6 is withdrawn. Spitball downloads no models and has no parakeet-specific code.

**Providers** (`transcription_provider`), in this UI order:
`"local"` (default) · `"deepgram"` · `"assemblyai"` · `"soniox"` · `"openai"`.

### local = whatever voxtype is set to
- Run plain `voxtype -q transcribe <win.wav>` with **no `--model`/`--engine`
  override**, so it uses the user's own voxtype config and active binary variant
  (Whisper base.en out of the box on Omarchy; Parakeet if they picked it in
  voxtype's model setup). Windowing per §1 local stays.
- Drop `local_model`, `local_engine`, `spitball local models`, `spitball local
  download`, and `download.json` entirely (remove any code/tests already written
  for them).
- New `spitball local info --json` → `{"installed": bool, "engine": "whisper",
  "model": "base.en", "message": "…"}` read from `voxtype config` output (or
  `~/.config/voxtype/config.toml` if easier; verify the real format).
- Settings UI, Local section: "Using voxtype: Whisper base.en" plus a
  **Change model…** button that runs `omarchy-voxtype-model` if present, else
  opens a terminal with `voxtype setup model`. If voxtype is missing: text "Install
  Omarchy's dictation (voxtype) to transcribe on this computer" and an **Install**
  button running `omarchy-voxtype-install` if present. After either button,
  re-read `local info`.
- `setup_needed` for local: `"Install dictation (voxtype) or pick a cloud service"`
  when voxtype is missing; otherwise empty.

### assemblyai (new)
- Key: `ASSEMBLYAI_API_KEY` env → `assemblyai_api_key` →
  `assemblyai_api_key_command`. Setting `assemblyai_model` (verify the current
  async model name in AssemblyAI's docs, e.g. "universal" / "best").
- Flow (verify every endpoint/field against current docs): upload the file
  (`POST https://api.assemblyai.com/v2/upload`) → create transcript with the
  upload URL, `language_code: "en"`, multichannel on → poll until completed →
  map utterances to the normalized structure (channel 0/1; if multichannel and
  speaker labels can't be combined, use multichannel and speaker 0).
- `check transcription` for assemblyai: a cheap authenticated GET (e.g. list
  transcripts with limit=1).

### soniox (new)
- Key: `SONIOX_API_KEY` env → `soniox_api_key` → `soniox_api_key_command`.
  Setting `soniox_model` (verify current async model, e.g. `stt-async-v5`).
- Flow (verify against current docs): upload file → create transcription with
  English language hint and speaker diarization → poll → fetch transcript tokens
  (with timestamps + speaker) → group into utterances. If Soniox has no
  multichannel mode, split the stereo file into two mono files as the openai
  provider does and diarize the far channel only. Delete the uploaded file and
  transcription afterward if the API supports it (privacy).
- `check transcription` for soniox: cheap authenticated GET.

### Settings keys added/removed
Add: `assemblyai_model`, `assemblyai_api_key(_command)`, `soniox_model`,
`soniox_api_key(_command)`. Remove: `local_model`, `local_engine`. `config get`
masks all `*_api_key` values as before.

### Tests
Offline: each new provider against a local fake HTTP server (upload, create,
poll incl. an error status, mapping). Live only when the provider's key env var is
set; otherwise skip. Local live test uses whatever voxtype is configured with.

## 8. Amendment: phase 1 = voxtype + Deepgram only (narrows §7)

Phase 1 ships exactly two providers: `"local"` (voxtype, default, per §7) and
`"deepgram"`. **OpenAI-compatible, AssemblyAI and Soniox move to phase 2** (the
user will get keys so they can be tested live first).

- Keep the provider abstraction (normalized utterances, `.transcript.json`,
  per-provider `check`) so phase 2 is additive.
- Remove openai/assemblyai/soniox from the code paths, settings keys, `config
  get` output, UI and tests. If any of that is already written, don't throw it
  away: save it as a patch under `docs/phase2/` (e.g.
  `docs/phase2/providers-openai-assemblyai-soniox.patch`, made with `git diff`
  or plain `diff -ruN`) with a short README noting its test status.
- Add `docs/ROADMAP.md` with a "Phase 2" section: OpenAI-compatible (incl.
  Groq / self-hosted Whisper servers, `gpt-4o-transcribe-diarize` speaker
  labels), AssemblyAI, Soniox — each needs an API key for live tests.
- Settings UI Transcription section: two choices, Local (voxtype) / Deepgram.
