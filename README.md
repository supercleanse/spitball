# Spitball

An [Omarchy](https://omarchy.org) bar plugin that records your calls, transcribes
both sides, and writes a summary — without you starting anything.

Spitball watches the microphone. When a call app opens it, an amber record button
appears in the bar. Click it (or turn on auto-record) and Spitball captures the call,
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
   sides on separate channels is what lets Deepgram tell your voice from everyone
   else's without guessing.

4. **Stops on its own.** About 8 seconds after the call app releases the mic,
   recording stops and processing starts. A recording under 60 seconds (10 for a
   manual start) gets discarded, on the theory that it's a Slack huddle you clicked
   out of, not a call.

5. **Transcribes.** The audio goes to Deepgram (`nova-3`, multichannel, diarized), so
   your side and theirs come back correctly labeled.

6. **Summarizes.** The transcript goes to any OpenAI-compatible chat endpoint (a
   local Ollama by default) for a title, summary, decisions, and action items. No
   model configured or reachable just means a transcript instead of a summary;
   `spitball reprocess <dir>` fills the summary in later.

Right-click the widget for the whole UI: start/stop, dismiss the current detection,
toggle auto-record, open the last call's summary, or open the calls folder.

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

### Restart the daemon

```bash
omarchy-shell supercleanse.spitball restart
```

Useful after editing `~/.config/spitball/config.json`, or if the daemon's gotten into
a bad state.

## What it needs

Already on a stock Omarchy install: `python3`, `ffmpeg`, `pactl` (part of
`libpulse`), and `notify-send`. Spitball's own code is Python standard library only —
no `pip install`, no virtualenv.

You also need a **Deepgram API key** — new accounts get free credit to start, and
`nova-3` prerecorded transcription runs at roughly $0.26/hour at the time of writing.
See [Deepgram's pricing](https://deepgram.com/pricing) for current rates.

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
`<timestamp>-<app>` and gets a slug of the generated title appended once transcription
and summarization are done.

Set `export_dir` and Spitball also copies the summary and full transcript, as one
markdown file, into that folder — handy for dropping calls straight into an Obsidian
vault or a notes tool.

## Configuration

Settings live in `~/.config/spitball/config.json`, an optional file — every key has a
default, and you only need to set the ones you want to change.

| Key | Default | Meaning |
|---|---|---|
| `calls_dir` | `~/Calls` | Where finished call folders go. |
| `export_dir` | `""` | Also copy each call's summary + transcript as one markdown file here. Empty means off. |
| `my_name` | your account's first name | The label used for your channel in the transcript and summary. |
| `deepgram_api_key` | `""` | Your Deepgram API key, in plain text. |
| `deepgram_api_key_command` | `""` | A shell command whose stdout is the key, for a password manager, e.g. `op read op://Private/Deepgram/credential`. |
| `summary_base_url` | `http://127.0.0.1:11434/v1` | Any OpenAI-compatible chat endpoint. |
| `summary_model` | `""` | Model name to request. Empty means the first model the endpoint lists. |
| `summary_api_key` | `""` | API key for `summary_base_url`, in plain text. |
| `summary_api_key_command` | `""` | A shell command whose stdout is that key, same idea as `deepgram_api_key_command`. |
| `summary_enabled` | `true` | Turn summarization off entirely and keep transcripts only. |
| `call_apps` | Zoom, Chrome, Chromium, Brave, Firefox, Slack, Teams, Discord, Signal, Webex, WhatsApp | Mic users that count as a call, matched case-insensitively against the process name; each key maps to a display name shown in the bar. |
| `detect_after_s` | `2` | Seconds the mic must stay open before a call counts as detected. |
| `end_after_s` | `8` | Seconds the app must be gone before a call counts as ended (rides out brief device switches). |
| `min_call_s` | `60` | Discard an auto-detected recording shorter than this. |
| `min_manual_s` | `10` | Discard a manually-started recording shorter than this. |
| `deepgram_model` | `nova-3` | Deepgram transcription model. |
| `opus_bitrate` | `32k` | ffmpeg's Opus encoding bitrate. |

The Deepgram key can also come from the `DEEPGRAM_API_KEY` environment variable,
which wins over both config keys — useful if you'd rather manage it outside the
config file entirely.

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
signal to diarize.

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
| `spitball reprocess <call-dir>` | Redo transcription and summary for one call folder. |
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

After changing QML (`Widget.qml`, `SpitballService.qml`), restart the shell:

```bash
omarchy restart shell
```

Tests run through `tests/run.sh`; pass `--live` to include anything that talks to a
real system (PipeWire, an actual model endpoint) instead of just fixtures.

## License

MIT. See [LICENSE](LICENSE).
