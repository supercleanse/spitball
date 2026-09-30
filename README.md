# Spitball

An [Omarchy](https://omarchy.org) bar plugin that records your calls, transcribes
both sides, and writes a summary — without you starting anything.

Spitball watches the microphone. When a call app opens it, an amber record button
appears in the bar. Click it and choose **Start recording** (or turn on auto-record) and
Spitball captures the call,
stops on its own when the call ends, and leaves you a transcript and a summary in
`~/Calls/`.

![Spitball's bar widget: idle, detected, recording and processing states](preview.png)

## What it does

1. **Detects.** A `pactl`-based watcher notices when Zoom, Chrome, Chromium, Brave,
   Firefox (including Google Meet running in any of those), Slack, Teams, Discord,
   Signal, Webex, or WhatsApp opens the microphone.

2. **Shows it in the bar.** The record button sits in the bar's center section, just
   left of the indicators. It stays hidden, like an inactive indicator, until you
   hover the section, except while a call is detected, recording, processing, or
   erroring, when it's always visible: amber for a detected call, a red dot with a
   running timer while recording.

3. **Records stereo Opus.** Left channel is your default microphone; right channel is
   whatever's playing through your default speakers or headphones. Keeping the two
   sides on separate channels is what lets the transcriber tell your voice from
   everyone else's without guessing.

4. **Stops on its own.** About 8 seconds after the call app releases the mic,
   recording stops and processing starts. A recording under 60 seconds (10 for a
   manual start) gets discarded, on the theory that it's a Slack huddle you clicked
   out of, not a call.

5. **Transcribes.** By default, on-device with [voxtype](https://voxtype.io) (ships
   with Omarchy) — audio never leaves your machine. Switch to Deepgram
   (`nova-3`, multichannel, diarized) for cloud transcription instead. See
   [Transcription providers](#transcription-providers) below. The copy of your mic
   the transcriber hears gets a rumble filter, and noise reduction when the room is
   loud; the recording itself is never altered. See [Audio and noise](#audio-and-noise).

6. **Summarizes.** The transcript goes to any OpenAI-compatible chat endpoint (a
   local Ollama by default) for a title, summary, decisions, and action items. No
   model configured or reachable just means a transcript instead of a summary;
   `spitball reprocess <dir>` fills the summary in later.

7. **Knows which meeting it was** (optional). Paste your calendar's secret iCal
   address and Spitball matches each call to the event it belongs to: the folder
   takes the event's title, the transcript starts with the meeting and its
   attendees, and the summarizer is told who was invited. See
   [Calendar](#calendar).

8. **Knows who said what.** With a matched meeting, one short model call puts the
   invitees' names on the far-side speakers from what people say ("thanks, Priya";
   "this is Alex"), and an unsure match stays visibly unsure: "Speaker 2 (probably
   Priya)". A 1:1 call needs no model at all. On the local provider, an optional
   on-device add-on tells the other side's voices apart first; Deepgram already
   does. `spitball speakers <dir> 2 "Priya Nair"` fixes a name by hand. See
   [Speakers](#speakers).

Any click on the widget, left or right, opens its menu: start recording, show the
live transcript and stop while recording, dismiss the current detection, toggle
auto-record, open the last call's summary, open the calls folder, or open Settings.
A click never stops a call by itself; Stop is always an explicit menu item or the
Live popup's button. The Live popup has a ✕ in its header to close it. When
something needs attention before Spitball can transcribe (no Deepgram key, no local
dictation installed), the widget shows a small "Set up" gear even while idle, and its
menu leads with **Set up transcription…**, which opens Settings on the Transcription
page. Switching the local model from Settings (see
[Transcription providers](#transcription-providers)) shows the same way: a small
download glyph while it runs, with the percentage in its tooltip.

## Settings

**Settings…** in the menu opens a centered overlay, the same kind of surface as
Omarchy's own menu and emoji picker: a card over a dimmed screen, with the sections
down the left and one page on the right. Esc, a click outside the card, or the ✕
closes it. When nothing on the page has focus, `j`/`k` or the arrow keys move between
sections, `1`–`9` jump straight to one, and Tab walks into the page's controls;
inside a text field, Esc hands focus back to the section list first.

| Page | What's on it |
|---|---|
| **General** | Your name, the spoken language, auto-record (with its consent note), the daemon's status and a Restart button. |
| **Recording** | The calls folder, the detection and ending timings, the minimum call lengths, the Opus bitrate, which apps count as a call, and which of them is on the mic right now. |
| **Transcription** | Local (voxtype) or Deepgram. Local: the voxtype model picker with its inline Switch confirmation and download progress, or an Install button if voxtype is missing. Deepgram: API key, model, Test, and the key command under Advanced. |
| **Live** | The live transcript on/off, the line cutoff, the fast-engine toggle, and whether the engine is installed (with an Install button). |
| **Audio** | Mic noise reduction for the transcriber (Off / Auto / On, with a line on each), the mic and speakers Spitball would record today, and under Advanced the background level Auto trips at. See [Audio and noise](#audio-and-noise). |
| **Speakers** | Naming the far side from the calendar invite, the on-device split of the far channel into separate voices (with an Install button), the most far-side voices to show, and which providers split speakers. See [Speakers](#speakers). |
| **Summary** | Summaries on/off, the endpoint, the model (a dropdown when the endpoint lists any), API key, Test, and the key command under Advanced. |
| **Calendar** | Matching on/off, the source (a secret iCal/ICS address stored like an API key, or your own command), a Test button that shows what a call starting now would match, whether the event title becomes the call title, what goes to the summarizer (attendee names on by default, the description off), and under Advanced the address command, your calendar email, and the feed refresh interval. See [Calendar](#calendar). |
| **Storage** | The notes-copy folder (`export_dir`) and a way into the calls folder. |
| **About** | Version, install path, a status snapshot, and the docs. |

Every change is saved the moment you make it (through `spitball config`, below). A
few actions open something outside the shell — the folder picker, the voxtype or
live-engine installer, the password prompt for a model switch — and the overlay
closes first so that window isn't hidden behind it; the folder picker brings it back
when you're done, the installers and model switch leave it closed until you reopen
it. From outside the shell, `omarchy-shell supercleanse.spitball-settings open`
(or `openSection transcription`) opens it too, handy for a keybinding.

## Live transcript

Starting a recording from the menu opens the **Live popup**, and **Show live
transcript** in the menu reopens it while recording: a chat-style transcript that fills
in as you talk, plus a **Stop recording** button and a ✕ that closes the popup while
the recording keeps going. Your lines sit
on the right, the other side's on the left, each with a small timestamp. It's powered
by a background live transcriber that runs on-device with voxtype regardless of
`transcription_provider` (see [Configuration](#configuration)'s `live_transcript` and
`live_max_window_s`) — it never costs money or sends audio anywhere mid-call, and
turning it off (`live_transcript: false`) just means the popup shows why it isn't
available instead of a transcript.

**Fast live engine.** Out of the box each line is one `voxtype transcribe` run, which
reloads the model every time, so lines land several seconds after each pause. Run
`spitball live setup` once to install a small venv (onnx-asr + onnxruntime, about
130 MB, under `~/.local/share/spitball/live-engine/`) that keeps voxtype's Parakeet
model loaded for the whole call. The popup then shows the sentence as it's spoken,
dimmed, trailing the speaker by well under a second, and locks it in about a second
after the pause. It uses whichever Parakeet model voxtype is set to (the streaming
`parakeet-unified-en-0.6b` included) and needs roughly that model's size in extra
memory during a call. `spitball live status` says whether it's on; if the engine
isn't installed, voxtype is on Whisper, or the worker fails, the live transcript
falls back to the voxtype path on its own. Once you stop, the popup shows "Recording saved;
transcribing…" briefly and closes on its own (or click ✕). If
`transcription_provider` is `local`, the finished live transcript is reused instead of
transcribing the whole call again — a normal full transcription still runs if it came
out too thin or too many parts failed. See CONTRACT.md for the full `live.json`/
`.live.json` file formats.

## Install

```bash
omarchy plugin add https://github.com/supercleanse/spitball --enable
```

Run interactively, `--enable` asks which bar section to use and defaults to center,
which puts Spitball immediately left of `omarchy.indicators`. Run non-interactively
(scripted, or with `--yes`), it lands in center without asking. To install without
enabling, drop `--enable` and run `omarchy plugin enable supercleanse.spitball`
later. To choose exactly where it sits:

```bash
omarchy plugin enable supercleanse.spitball --section center --before omarchy.indicators
```

See `omarchy plugin enable --help` for the full set of placement flags.

Then set a Deepgram key (see [Configuration](#configuration)) and, if you want
summaries, have something OpenAI-compatible listening — a local Ollama at
`127.0.0.1:11434` needs no configuration at all.

### Update

```bash
omarchy plugin update supercleanse.spitball
```

### Remove

```bash
omarchy plugin remove supercleanse.spitball
```

That removes the plugin itself. Spitball's own settings, state, and the optional
live-engine venv stay behind until you delete them:

```bash
rm -rf ~/.config/spitball ~/.local/state/spitball ~/.local/share/spitball
```

Your recorded calls (`~/Calls/` by default) are never touched. If you let Spitball
switch voxtype's model, voxtype keeps that model; change it back with `voxtype setup`.

### Restart the daemon

```bash
omarchy-shell supercleanse.spitball restart
```

Useful after editing `~/.config/spitball/config.json`, or if the daemon's gotten into
a bad state.

## What it needs

Already on a stock Omarchy install: `python3`, `ffmpeg`, `pactl` (part of
`libpulse`), `notify-send`, and [voxtype](https://voxtype.io) (Omarchy's own
dictation tool — Spitball's default transcription provider runs on it, on-device,
with no key and no setup). Spitball's own code is Python standard library only and
runs on the system `python3`, with no `pip install` needed.

Optional, for the fast live transcript: `spitball live setup` creates a small
virtualenv at `~/.local/share/spitball/live-engine/venv` (about 130 MB) and installs
[onnx-asr](https://github.com/istupakov/onnx-asr), onnxruntime, numpy, and
sentencepiece into it from PyPI. It uses [uv](https://docs.astral.sh/uv/) when it's
on your PATH, otherwise `python3 -m venv` and pip. Nothing outside that folder is
installed, and deleting the folder removes it. Without it, the live transcript still
works, just a few seconds slower (see [Live transcript](#live-transcript)).

Switching voxtype between its Whisper and Parakeet engines from Settings asks for
your password (a graphical `pkexec` prompt, or `sudo` in a terminal as a fallback),
because voxtype's own `voxtype setup onnx` needs root. Spitball then restarts the
voxtype user service (`systemctl --user restart voxtype`). Nothing else Spitball does
needs elevated rights.

Want cloud transcription instead? Switch `transcription_provider` to `deepgram`
and set a **Deepgram API key** — new accounts get free credit to start, and
`nova-3` prerecorded transcription runs at roughly $0.26/hour at the time of
writing. See [Deepgram's pricing](https://deepgram.com/pricing) for current rates,
and [Transcription providers](#transcription-providers) below.

Summaries are optional and need one more thing: something speaking the OpenAI chat
API. A local [Ollama](https://ollama.com) works out of the box at its default port;
LM Studio, OpenAI, and OpenRouter all work too (see `summary_base_url` below).

## Files per call

Each call gets its own folder, `~/Calls/<YYYY-MM-DD-HHMM>-<app>-<title-slug>/`:

| File | Contents |
|---|---|
| `audio.opus` | The stereo recording (left = you, right = the room). |
| `transcript.md` | Full transcript, speaker-labeled and timestamped. |
| `summary.md` | Title, summary, decisions, action items, open questions. |

The folder isn't named with a title until processing finishes — it starts as
`<timestamp>-<app>` and gets a slug of the title appended once transcription and
summarization are done: the matched calendar event's title when there is a confident
match (see [Calendar](#calendar)), otherwise the summarizer's. A few dot-files sit
beside them: `.meta.json` (the call's facts, plus the calendar candidates snapshotted
when recording started), `.transcript.json` (the cached transcript, the matched meeting
and its attendees, who each far-side speaker resolved to, which mic noise reduction
ran, and whether the speaker split ran), and `.live.json` (the live transcript, when
it ran).

Set `export_dir` and Spitball also copies the summary and full transcript, as one
markdown file, into that folder — handy for dropping calls straight into an Obsidian
vault or a notes tool.

## Transcription providers

Set `transcription_provider` to pick one:

- **`local`** (default) — runs on voxtype, on-device, no key needed. Spitball never
  manages voxtype's model or engine choice itself; it just runs whatever you already
  have voxtype configured with (Whisper `base.en` out of the box on Omarchy, or
  Parakeet if you've picked it in voxtype's own setup). `spitball local info` shows
  what's active; `spitball local models` lists every model voxtype knows how to
  download with size/language/installed info.

  The Transcription page in Settings has one Model dropdown, with **Parakeet
  (unified, English)** marked Recommended. It's the one streaming-capable model:
  switching to it also turns on voxtype's `parakeet.streaming`, so dictation
  types while you talk instead of after you stop, and writes the streaming
  window sizes voxtype needs into its `[parakeet]` config. Switching to any
  other Parakeet model turns streaming back off. Language coverage varies by
  model:
  - **Parakeet (unified, English)** (recommended) — English only, about 2.4 GB.
  - **Parakeet v3** (and its int8 variant) — 25 European languages.
  - **Parakeet v2** (and its int8 variant) — English only.
  - **Whisper** multilingual models (the plain `tiny`/`base`/`small`/`medium`/
    `large-v3`/`large-v3-turbo`, without a `.en` suffix) — around 99 languages;
    the `.en` variants are English-only and slightly more accurate for it.

  Picking a different model in the dropdown shows an inline confirmation
  ("Switch to Parakeet v3 (int8)? Downloads about 640 MB and asks for your
  password once. Also changes Omarchy dictation.") — nothing switches until you
  click **Switch**. `spitball local set-model <name>` (what that button calls)
  then runs the whole switch **in the background**: it never blocks, and its
  progress (`$XDG_RUNTIME_DIR/spitball/model.json`, see CONTRACT.md) shows both
  in Settings, if you reopen it, and as a small download glyph on the bar
  widget itself while it runs. Normally there's no terminal at all — just
  Omarchy's own graphical password prompt if the engine needs to change (e.g.
  Whisper → Parakeet), then a background download. It only opens a floating
  terminal (the original, fully interactive approach) as a fallback, when
  there's no way to draw that graphical prompt at all. Either way, Spitball's
  own process never runs `sudo`/`pkexec` itself or edits voxtype's config
  directly. If voxtype isn't installed at all, the bar shows a "Set up" gear
  with a hint to install it or switch providers.

  voxtype hears the other side as one voice ("Them"). `spitball diarize setup`
  adds an on-device speaker split that tells the far-side voices apart before
  they are transcribed — see [Speakers](#speakers).
- **`deepgram`** — cloud, multichannel + diarized, so the far side can have several
  distinct speakers without any add-on. See `deepgram_model`/`deepgram_api_key(_command)` below.

More providers (an OpenAI-compatible endpoint, AssemblyAI, Soniox) are planned —
see `docs/ROADMAP.md`.

Every provider transcribes into the same shape, cached as `.transcript.json` in the
call folder so `spitball reprocess <dir>` doesn't re-transcribe unless you pass
`--retranscribe`. `language` picks the spoken language: `"en"` (default), `"auto"`
(provider-dependent detection), or an ISO code.

## Audio and noise

Spitball records your mic as it is. What changes is the copy the transcriber hears:

- **Always:** a high-pass filter at 80 Hz on the mic copy takes out desk rumble, fan
  hum, and handling thumps below the voice band. The far side (the monitor of your
  speakers) is left exactly as recorded: it is already the call app's processed
  output, and cleaning it again only loses detail.
- **When the room is noisy:** `mic_denoise` runs RNNoise (ffmpeg's `arnndn` filter
  with the model in `models/rnnoise/`, blended 70/30 with the original) over a
  temporary copy of the mic channel before transcription. If the model file is
  missing or the filter fails, ffmpeg's built-in `afftdn` runs instead.

`mic_denoise` has three settings (the Audio page in Settings, or `spitball config set
mic_denoise off|auto|on`):

| Value | What happens |
|---|---|
| `off` | Nothing beyond the high-pass. |
| `auto` (default) | Spitball measures the mic's background level (the quietest tenth of the call's mic audio, in 50 ms slices) and denoises only when it is above `mic_noise_floor_db`, −45 dBFS by default. A headset in a quiet room sits near −60 and is left alone; a laptop fan lands around −45 to −40; a cafe or an open office reads above −35. |
| `on` | Always denoise the mic copy. |

Why the default is a gate and not simply on: Whisper and Parakeet were trained on
noisy audio, and more than one study has found that denoising clean audio makes their
transcripts worse, not better (the research is summarized in `docs/SPEC-v2.md`). A
check on this machine with Parakeet and synthetic noise found the filter neither
helped nor hurt at the noise levels tried, so the gate is there to keep a clean mic
exactly as it was. Judge it on your own calls: the `mic_denoise` block below tells
you what ran, and `reprocess --retranscribe` lets you compare.

The live transcript follows the same setting. Each few-second slice of the mic is
measured as it arrives, and once the running background level crosses the threshold
the slices are denoised the same way (with 3 dB of hysteresis so a call hovering at
the threshold doesn't flip between lines).

What ran is recorded in `.transcript.json` (and `.live.json`) as a `mic_denoise`
block: the mode, whether it applied, which filter, the measured background and
speaking levels in dBFS, and the threshold. `spitball reprocess <dir> --retranscribe`
transcribes again with the current setting, so you can compare a call with it off and
on. The Deepgram provider uploads the recording as it is; the setting does not apply
to it (Deepgram's own advice is not to preprocess).

**Whisper and silence.** Whisper is known to invent text over audio that has no
speech in it. When voxtype is on a Whisper model, Spitball segments both channels
with a stricter speech check: the pause detector's gate rises with the measured
background, so steady noise still splits into pauses, and any window with nothing
louder than the room in it is skipped. Parakeet doesn't hallucinate that way and keeps the plain
segmentation.

### Echo from laptop speakers

Noise reduction cannot remove the other side's voice from your mic. It is speech, and
the filter keeps speech. On built-in speakers that bleed makes both channels repeat
each other; Spitball drops mic lines that duplicate far-side lines in time and
wording, but the real fix is echo cancellation at the source, which needs the
speaker signal as a reference. PipeWire ships exactly that. Create
`~/.config/pipewire/pipewire.conf.d/echo-cancel.conf`:

```
context.modules = [
  { name = libpipewire-module-echo-cancel
    args = {
      library.name = aec/libspa-aec-webrtc
      aec.args = { webrtc.noise_suppression = true }
      source.props = { node.name = "echo-cancel-source" node.description = "Echo-canceled mic" }
      sink.props   = { node.name = "echo-cancel-sink"   node.description = "Echo-canceled speakers" }
    }
  }
]
```

Then `systemctl --user restart pipewire pipewire-pulse wireplumber` and make the new
source and sink your defaults (`wpctl status` lists them, `wpctl set-default <id>`
picks one). Spitball records the default source, so it gets the cleaned mic with no
change on its side, and so does your call app. (Zoom, Meet, Teams, and Discord cancel
echo inside the app already; the module matters for the recording, which taps the
mic before the app does.) [EasyEffects](https://github.com/wwmm/easyeffects)
(`pacman -S easyeffects`) wraps the same WebRTC echo canceler, RNNoise, and
DeepFilterNet behind a GUI and works with Spitball the same way: whatever it makes
the default source is what gets recorded. Headphones avoid the problem entirely.

## Calendar

Turn on **Match calls to calendar events** on the Calendar page and Spitball works out
which meeting each call was. There's no Google sign-in and no OAuth client to set up:

1. In Google Calendar, open Settings → your calendar → **Integrate calendar** and copy
   the **Secret address in iCal format**. Any other ICS/webcal address works the same
   way (Outlook's "Publish calendar", Fastmail, Nextcloud, Proton…), with whatever
   attendee detail that provider puts in its feed.
2. Paste it into **Secret iCal address** and click **Test**.

The address is a credential: anyone holding it can read that calendar until you
reset it in the same settings page. So Spitball stores it like an API key
(`spitball config set-secret calendar_ics_url`, masked everywhere, never logged),
and a `calendar_ics_url_command` variant fetches it from a password manager instead.
The feed (your whole calendar, several MB for an old one) is downloaded at most once
per `calendar_cache_ttl_s` (15 minutes) into `~/.local/state/spitball/calendar/`, mode
600, and a stale copy is used when the download fails.

**What a confident match does.** The call folder is named after the event
(`2026-09-30-1400-chrome-weekly-sync`), `transcript.md` and `summary.md` open with
`**Meeting:**`, `**When:**`, and `**Attendees:**` lines, the summarizer is told who
was on the invite (and, only if you turn it on, the event description), and the
attendee list is kept in `.transcript.json` for speaker naming. Turn off **Use the
event title as the call title** to keep the summarizer's own title and add only the
header. A weak match never renames anything: the header just says
`**Calendar:** no confident match (2 candidates)`.

**How matching works.** When recording starts, the daemon snapshots every event whose
time touches the call (from 15 minutes before its start to 10 minutes after its end)
into the call's `.meta.json`, along with any Google Meet code visible in a window
title (Chromium titles a Meet tab "Meet – abc-defg-hij"). All-day events, canceled
ones, ones marked free, focus time, out-of-office, working-location and birthday
entries, and invites you declined are dropped. The rest are scored: a Meet code that
matches the event's link is decisive; otherwise it's whether the meeting link fits
the app on the mic (a Meet link with a browser, a Zoom link with Zoom), whether the
recording started during the event and how far from its start, how many other people
were invited, whether you accepted, and, once the call has ended, how much of the
recording fell inside the event, which is what settles back-to-back meetings. The
best event has to clear a threshold and beat the runner-up by a margin, or there is
no match. Recurring series are expanded on the spot (weekly, daily, monthly,
yearly, `INTERVAL`, `COUNT`, `UNTIL`, `BYDAY` including "last Wednesday", `EXDATE`,
moved and canceled instances) in the event's own time zone.

Calendar trouble (no network, a reset address, a slow feed) never delays or blocks a
recording: the lookup runs in the background, and a failure is just noted in
`.meta.json`. `spitball calendar test` shows the match for a call starting now,
`--at 14:30` (or a full date/time) for another moment, `--meet abc-defg-hij` /
`--app Zoom` to add the context a real call would have, and `--json` for the raw
picture. Got it wrong? `spitball reprocess <dir> --event <id>` pins one of the
snapshot's candidates (ids are in `.meta.json` and the `--json` output) and
`--no-event` clears the match; both re-render the transcript and summary.

**Your own source.** Set **Source** to *Your own command* (`calendar_source:
"command"`) and `calendar_command` to any shell command that prints a JSON array of
events — the shape is in [CONTRACT.md](CONTRACT.md#calendar-events); only `title`,
`start`, and `end` are required. The command gets the window as
`SPITBALL_WINDOW_START`/`SPITBALL_WINDOW_END`. This is how khal/vdirsyncer, gcalcli,
a CalDAV script, or an Evolution Data Server one-liner plugs into the same matcher.

## Speakers

Your own channel is always labeled with your name. The other side is one voice
("Them") or several ("Speaker 1", "Speaker 2", …), depending on the provider and on
the optional split below. Two things then put names on those labels, both on the
Speakers page.

**Naming from the invite** (`speaker_names`, on by default). When a call has a
confident calendar match, Spitball hands the transcript (with its neutral labels) and
the invitees' names to the summary endpoint once, before the summary, and asks which
"Speaker N" is which invitee and why. Only what people actually say counts: someone
introducing themselves, being addressed by name right before or after their turn,
or you addressing them. A name that isn't on the invite is thrown away; a name
claimed for two speakers makes both unsure. A sure match shows the name; an unsure
one reads `Speaker 2 (probably Priya Nair)`, in the transcript and in the summary's
own wording; no evidence keeps `Speaker 2`. A 1:1 call (one other invitee, one
far-side voice) is named with no model call at all. No pitch or gender guessing,
and no voiceprints: nothing about anyone's voice is remembered between calls.

Got a name wrong or missing? `spitball speakers <call-dir>` lists the far-side
speakers with what each resolved to and the evidence; `spitball speakers <call-dir>
2 "Priya Nair"` names one by hand and re-renders `transcript.md`, `summary.md` (its
wording included), and the export copy on the spot, without another model call. A
hand-set name is final: a later `spitball reprocess` keeps it and only re-resolves
the others. `--clear` goes back to automatic. If you want a fresh summary written
with the corrected names, run `spitball reprocess <call-dir>` afterward.

**Telling voices apart on this computer** (`speaker_split`, on by default, no
effect until installed). Deepgram splits the far channel on its own. The local
provider can too, once you click **Install** on the Speakers page (or run
`spitball diarize setup`): it adds the [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)
wheel (about 100 MB) to the same virtualenv the fast live engine uses,
`~/.local/share/spitball/live-engine/`, and downloads two small models (pyannote's
segmentation-3.0 and a 3D-Speaker embedding model, about 30 MB together) from the
k2-fsa GitHub releases, each verified against a pinned hash. No Hugging Face account
or token, no PyTorch. Provenance and licenses are in `models/diarization/README.md`.

With it installed, the far channel (only the far channel; your side is already
separate) is diarized before transcription, each speech window is cut where the
speaker changes, and every line carries the id of one voice. When the invite says
how many other people were there, that count drives the clustering, which is the
single biggest quality lever; when it doesn't, the clustering picks a count on its
own and the result is capped at **Most far-side voices to show** (`speaker_max`,
6). A call whose invite lists exactly one other person is never split. A voice with
only a few words (an "mm-hm" the segmenter split off, a notification sound) folds
into the voice speaking around it; so do voices beyond the cap. If the add-on is
missing, the worker fails, or the far channel has no speech, the transcript keeps
today's single "Them" and `.transcript.json`'s `diarization` block says why. Expect
clean splits for two to four distinct headset voices, and merges or swaps for very
similar voices, brief speakers, and several people sharing one room microphone; the
header's `**Note:** the invite lists 3 other people; 5 voices were found on the far
side.` line is the tell when a split went wrong. It costs a few seconds of CPU per
minute of far-side speech.

## Configuration

Settings live in `~/.config/spitball/config.json`, an optional file — every key has a
default, and you only need to set the ones you want to change.

| Key | Default | Meaning |
|---|---|---|
| `calls_dir` | `~/Calls` | Where finished call folders go. |
| `export_dir` | `""` | Also copy each call's summary + transcript as one markdown file here. Empty means off. |
| `my_name` | your account's first name | The label used for your channel in the transcript and summary. |
| `auto_record` | `false` | Start recording automatically the moment a call is detected. Same setting `spitball auto on\|off\|toggle` flips. |
| `language` | `"en"` | Spoken language: an ISO code, or `"auto"` for provider-dependent detection (deepgram: its own language-detection mode; local: whatever voxtype's active model supports). |
| `transcription_provider` | `"local"` | `"local"` (voxtype, on-device) or `"deepgram"` (cloud). See [Transcription providers](#transcription-providers). |
| `deepgram_api_key` | `""` | Your Deepgram API key, in plain text. |
| `deepgram_api_key_command` | `""` | A shell command whose stdout is the key, for a password manager, e.g. `op read op://Private/Deepgram/credential`. |
| `deepgram_model` | `nova-3` | Deepgram transcription model. |
| `summary_base_url` | `http://127.0.0.1:11434/v1` | Any OpenAI-compatible chat endpoint. |
| `summary_model` | `""` | Model name to request. Empty means the first model the endpoint lists. |
| `summary_api_key` | `""` | API key for `summary_base_url`, in plain text. |
| `summary_api_key_command` | `""` | A shell command whose stdout is that key, same idea as `deepgram_api_key_command`. |
| `summary_enabled` | `true` | Turn summarization off entirely and keep transcripts only. |
| `call_apps` | Zoom, Chrome, Chromium, Brave, Firefox, Slack, Teams, Discord, Signal, Webex, WhatsApp | Mic users that count as a call, matched case-insensitively against the process name; each key maps to a display name shown in the bar. |
| `live_transcript` | `true` | Show a live, chat-style transcript in the bar's Live popup while recording (see [Live transcript](#live-transcript)). Always uses the local provider, regardless of `transcription_provider` -- it never costs money or sends audio anywhere mid-call. |
| `live_max_window_s` | `12` | A live segment with no pause yet is cut here regardless, so one long uninterrupted stretch of talk still shows a line before the call ends. |
| `live_engine` | `true` | Use the fast live engine once `spitball live setup` has installed it (see [Live transcript](#live-transcript)). `false` goes back to one `voxtype transcribe` per line. |
| `detect_after_s` | `2` | Seconds the mic must stay open before a call counts as detected. |
| `end_after_s` | `8` | Seconds the app must be gone before a call counts as ended (rides out brief device switches). |
| `min_call_s` | `60` | Discard an auto-detected recording shorter than this. |
| `min_manual_s` | `10` | Discard a manually-started recording shorter than this. |
| `opus_bitrate` | `32k` | ffmpeg's Opus encoding bitrate. |
| `mic_denoise` | `"auto"` | Noise reduction on the copy of the mic the transcriber hears: `"off"`, `"auto"` (only when the measured background is above `mic_noise_floor_db`), or `"on"`. Never touches the recording or the far side. See [Audio and noise](#audio-and-noise). |
| `mic_noise_floor_db` | `-45` | The background level (dBFS) above which `"auto"` denoises. Lower it to denoise more often. |
| `calendar_enabled` | `false` | Match each call to a calendar event (see [Calendar](#calendar)). |
| `calendar_source` | `"ics"` | `"ics"` (the secret address below) or `"command"` (`calendar_command`). |
| `calendar_ics_url` | `""` | The secret iCal/ICS/webcal address. A secret: `set-secret` only, masked in `config get`. |
| `calendar_ics_url_command` | `""` | A shell command whose stdout is that address, for a password manager. |
| `calendar_command` | `""` | A shell command printing a JSON array of events (shape in CONTRACT.md); used when `calendar_source` is `"command"`. |
| `calendar_cache_ttl_s` | `900` | Re-download the feed once the cached copy is older than this many seconds. |
| `calendar_prefer_event_title` | `true` | On a confident match, the event's title becomes the call's title (folder, transcript, summary). `false` keeps the summarizer's title and adds only the meeting header. |
| `calendar_names_to_summary` | `true` | Tell the summarizer who was on the invite. |
| `calendar_description_to_summary` | `false` | Also send the event description to the summarizer. Off by default: it can carry private text. |
| `calendar_my_email` | `""` | Your address on the calendar, so your own response is read (declined invites are skipped). Empty: the address on nearly every invite in the feed is taken as yours. |
| `speaker_names` | `true` | Put invitees' names on the far-side speakers, from what people say, when the call matched a meeting. One short call to the summary endpoint; a 1:1 needs none. See [Speakers](#speakers). |
| `speaker_split` | `true` | Tell far-side voices apart on the local provider, once `spitball diarize setup` has installed the add-on. Never runs for a 1:1; any failure keeps one "Them". |
| `speaker_max` | `6` | The most far-side voices a transcript shows (1–12). The invite's headcount is used when known, capped here; extra or tiny voices fold into their neighbors. |

The Deepgram key can also come from the `DEEPGRAM_API_KEY` environment variable,
which wins over both config keys — useful if you'd rather manage it outside the
config file entirely. Same idea for `summary_api_key`, via `summary_api_key_command`.

Every key above has a control in [Settings](#settings). You can also edit
`config.json` by hand (restart the daemon afterward — see
[Restart the daemon](#restart-the-daemon)), or use `spitball config`:

```bash
spitball config get --json                       # effective settings; *_api_key values are masked
spitball config set my_name Morgan
spitball config set transcription_provider deepgram
echo -n "your-deepgram-key" | spitball config set-secret deepgram_api_key
spitball config unset export_dir                 # back to the default
```

`config set`/`set-secret`/`unset` write atomically, make `config.json` mode `600`
once it holds a secret, and reload the running daemon automatically (falls back to
nothing happening — no crash — if the daemon isn't up). `set` rejects unknown keys
and the secret keys themselves (`deepgram_api_key`, `summary_api_key`,
`calendar_ics_url`) — those go through `set-secret`, which reads the value from stdin
so it never lands in your shell history or `ps` output.

`spitball check transcription --json` and `spitball check summary --json` test the
configured provider/endpoint for real (a live network call) and report `{"ok", "message"}`
(summary also lists `"models"`). `spitball calendar test --json` does the same for the
calendar source and adds the match for a call starting now.

## Crash recovery

If the daemon dies mid-recording (the shell crashes, the session ends uncleanly), the
next start finds the orphaned `ffmpeg` process, stops it, and finishes that call's
`audio.opus` cleanly. It also walks `~/Calls/` for any folder that has audio
but no `summary.md` and picks up transcription and summarization where they left off.
Nothing you recorded gets lost to a bad restart.

## Privacy and consent

Audio never leaves your machine except for one upload: the finished recording goes to
Deepgram for transcription. That request sets `mip_opt_out=true`, which opts you out
of Deepgram's model-improvement program — see
[Deepgram's privacy policy](https://deepgram.com/privacy) for what they do with
submitted audio. The transcript then goes wherever `summary_base_url` points, which
is a local model on your own machine by default and stays there unless you point it
somewhere else.

With the calendar on, the feed is read into `~/.local/state/spitball/calendar/` (mode
600) and the events around each call are written into that call's `.meta.json`. The
names of the people on the invite go to the summary endpoint along with the
transcript (`calendar_names_to_summary`, on by default — turn it off if that endpoint
is not your own machine); the event description goes only if you turn
`calendar_description_to_summary` on. Attendee email addresses stay in the dot-files
and never appear in `transcript.md` or `summary.md` unless an attendee has no name on
the invite.

Speaker naming sends the same two things (the transcript and the invitees' names and
addresses) to that same endpoint once more, before the summary; `speaker_names:
false` turns it off. The on-device speaker split runs entirely on your machine and
keeps nothing between calls: no voice profiles, no enrollment, no guesses about
anyone's gender. The names Spitball prints come only from the invite and from what
people said.

Recording laws vary by place — some require only your own consent, others require
everyone on the call to consent. Check your jurisdiction, and tell people you're
recording.

## Troubleshooting

**Their side is missing from the recording.** Spitball records your default output
device's monitor. Check `pactl get-default-sink` — if it's pointed at a dock or
headset you've since unplugged, that's a silent monitor.

**No call gets detected.** Run `pactl list source-outputs` while the call is live and
look at the stream's `application.name` and `application.process.binary`. If the app
isn't there under a name Spitball recognizes, add it to `call_apps` in
`~/.config/spitball/config.json`.

**Is the daemon even running?**

```bash
spitball status
```

Logs come through `notify-send` for user-facing errors; for anything else, run the
daemon in a terminal (`python3 -I bin/spitball daemon`) to see its stderr directly.

**Recorder failed to start.** The daemon reads `ffmpeg`'s own log and surfaces its
last line as the error — usually a PipeWire device that's gone missing. Confirm both
`pactl get-default-source` and `pactl get-default-sink` return something.

**Echo on laptop speakers.** On built-in speakers, the mic also picks up the other
side's voice, so both channels partially repeat each other. Spitball filters
mic-channel text that overlaps far-side text in time and wording, but it isn't
perfect — headphones avoid the problem at the source and give Deepgram a cleaner
signal to diarize, and PipeWire's echo cancellation fixes it for real; see
[Echo from laptop speakers](#echo-from-laptop-speakers).

**Words nobody said, in the quiet parts.** That is Whisper filling silence or noise
with text. Spitball's Whisper-path speech check skips windows with nothing above the
room's level in them, and `mic_denoise` lowers what the model hears in a noisy room;
switching voxtype to Parakeet (Transcription page) avoids it altogether. Check the
`mic_denoise` block in `.transcript.json` to see what ran.

## CLI

`bin/spitball` is the plugin's own CLI. Symlink it onto your `PATH` for convenience:

```bash
ln -s ~/.config/omarchy/plugins/supercleanse.spitball/bin/spitball ~/.local/bin/spitball
```

| Command | Effect |
|---|---|
| `spitball start` | Start recording now: follows a detected call if one is live, else manual. |
| `spitball stop` | Stop recording; processing begins in the background. |
| `spitball toggle` | Start if idle, stop if recording. |
| `spitball dismiss` | Ignore the current detection (or clear an error) without recording. |
| `spitball auto on\|off\|toggle` | Set auto-record: start automatically the moment a call is detected. |
| `spitball open-last` | Open the last call's `summary.md`. |
| `spitball open-folder` | Open `~/Calls` in the file manager. |
| `spitball status [--json]` | Print the current state. |
| `spitball reprocess <call-dir> [--retranscribe] [--event <id> \| --no-event]` | Redo transcription and summary for one call folder. `--retranscribe` ignores the cached transcript and calls the provider again; `--event`/`--no-event` pin or clear the calendar match by hand. |
| `spitball config get\|set\|set-secret\|unset` | Read/edit settings. See [Configuration](#configuration). |
| `spitball check transcription\|summary [--json]` | Test the configured transcription provider or summary endpoint for real. |
| `spitball calendar test [--at TIME] [--app APP] [--meet CODE] [--refresh] [--json]` | Which calendar event a call starting now (or at `TIME`) would match, with every candidate and its score. See [Calendar](#calendar). |
| `spitball local info\|models\|set-model` | What voxtype is configured with, every model it can download, or switch it to one. See [Transcription providers](#transcription-providers). |
| `spitball live setup\|status [--json]` | Install the fast live-transcript engine, or show whether it's on and which model it loads. See [Live transcript](#live-transcript). |
| `spitball speakers <call-dir> [--json]` | List a call's far-side speakers: label, resolved name, confidence, source, evidence, talk time. See [Speakers](#speakers). |
| `spitball speakers <call-dir> <n> "Name"` / `--clear` | Name speaker `n` by hand (or go back to automatic) and re-render `transcript.md`, `summary.md`, and the export copy. No model call. |
| `spitball diarize setup\|status [--json]` | Install the on-device speaker split (sherpa-onnx + two small models, into the live-engine venv), or show whether it's installed. |
| `spitball pick-folder [--title T]` | Native folder chooser; prints the chosen path (used by the settings panel). |
| `spitball daemon` | Run the service directly. `SpitballService.qml` does this for you; you shouldn't need to. |

Full state-file and CLI contract in [CONTRACT.md](CONTRACT.md).

## Development

The repository *is* the plugin — there's no separate build step. For live editing,
symlink it into place instead of using `omarchy plugin add`:

```bash
ln -s /path/to/spitball ~/.config/omarchy/plugins/supercleanse.spitball
omarchy-shell shell rescanPlugins
```

After changing Python (`spitball/*.py`, `bin/spitball`), restart just the daemon:

```bash
omarchy-shell supercleanse.spitball restart
```

After changing QML (`Widget.qml`, `SpitballService.qml`, `LivePopup.qml`,
`SettingsWindow.qml`, anything under `settings/`), restart the shell:

```bash
omarchy restart shell
```

Tests run through `tests/run.sh`; pass `--live` to include anything that talks to a
real system (PipeWire, an actual model endpoint) instead of just fixtures. Audio
tests synthesize their own clips with ffmpeg at run time (nothing under `tests/`
ships audio); `models/rnnoise/sh.rnnn` is the one binary asset in the repository. The
speaker split's worker is exercised against a fake in the normal run; set
`SPITBALL_DIARIZE_TEST_DIR` to a directory laid out like `spitball diarize setup`
leaves `~/.local/share/spitball/live-engine/` (a `venv/` with sherpa-onnx and
`models/diarization/` with the two pinned models) to also run it for real on a
two-voice clip built from the speech samples in `tests/.cache/`. The
settings overlay can be rendered without touching your desktop:
`tests/offscreen/render.sh --fake-data <out-dir>` runs Quickshell offscreen
against a fake CLI and writes one PNG per page. The layout is `SettingsWindow.qml`
(the layer-shell window: scrim, focus, dismissal) over `settings/SettingsCard.qml`
(header, nav, page loader), with `settings/SettingsStore.qml` holding every CLI
round trip and one `settings/<Name>Page.qml` per section. To add a section, add an
entry to `SETTINGS_SECTIONS` in `Model.js` and a page file that extends
`SettingsPage`; the nav, the digit keys, and the tests pick it up from there.

## License

MIT. See [LICENSE](LICENSE). `models/rnnoise/sh.rnnn` is redistributed unchanged
from [rnnoise-models](https://github.com/GregorR/rnnoise-models); its provenance and
terms are in `models/rnnoise/README.md`.
