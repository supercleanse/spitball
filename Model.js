.pragma library

// Pure helpers for Spitball widget. No QML types in here, so state
// parsing and formatting stay easy to read apart from the layout. Mirrors
// programs/Spitball/CONTRACT.md -- this is the only file that needs to
// change if the state-file shape ever does.

var KNOWN_STATES = ["offline", "idle", "detected", "recording", "processing", "error"]

function emptyState() {
  return {
    state: "offline", app: "", started_at: 0, auto_record: false,
    message: "", last_call: null, updated_at: 0, setup_needed: ""
  }
}

// Parses the daemon's state.json text. Per the contract: a missing or
// unparseable file is treated as {"state": "offline"}. Always returns a
// fully-populated object (never null) so callers can bind straight to its
// fields without null-checking every read.
function parseState(text) {
  var base = emptyState()
  try {
    var s = JSON.parse(String(text || ""))
    if (!s || typeof s !== "object") return base
    for (var k in base) if (s[k] === undefined) s[k] = base[k]
    if (KNOWN_STATES.indexOf(s.state) === -1) s.state = "offline"
    if (s.last_call !== null && typeof s.last_call !== "object") s.last_call = null
    if (typeof s.setup_needed !== "string") s.setup_needed = ""
    return s
  } catch (e) {
    return base
  }
}

function pad2(n) {
  return n < 10 ? "0" + n : String(n)
}

// "m:ss" under an hour, "h:mm:ss" at or past one hour, per the contract.
function elapsedLabel(startedAt, nowMs) {
  var started = Number(startedAt) || 0
  if (started <= 0) return "0:00"
  var secs = Math.max(0, Math.floor(nowMs / 1000 - started))
  var h = Math.floor(secs / 3600)
  var m = Math.floor((secs % 3600) / 60)
  var s = secs % 60
  return h > 0 ? (h + ":" + pad2(m) + ":" + pad2(s)) : (m + ":" + pad2(s))
}

// True when the widget should show the "needs setup" gear state instead of
// its normal idle glyph: idle, with the daemon reporting a non-empty reason
// (missing key, no local model downloaded, voxtype not installed...). Only
// idle -- "offline" means the daemon hasn't reported anything at all yet,
// and every other state already has its own always-visible glyph.
function needsSetup(st) {
  return st.state === "idle" && !!st.setup_needed
}

function tooltipFor(st) {
  if (needsSetup(st)) return st.setup_needed
  switch (st.state) {
    // Any click on the bar opens the menu (Start/Stop, the live transcript,
    // Settings); per docs/SPEC-live-transcript.md a click never stops a
    // recording by itself.
    case "detected": return "Call detected in " + (st.app || "an app") + ": click for options"
    case "recording": return "Recording " + (st.app || "call") + ": click for options"
    case "processing": return st.message || "Processing…"
    case "error": return st.message || "Spitball error"
    case "idle": return "No call in progress: click for options"
    default: return "Spitball: daemon not running"
  }
}

// Clips text for a popup menu row so a long call title can't blow out the
// panel width -- the shared Button component doesn't elide its own label.
function truncate(text, max) {
  var t = String(text || "")
  return t.length > max ? t.slice(0, max - 1) + "…" : t
}

function lastCallLabel(lastCall) {
  if (!lastCall || !lastCall.title) return "Open last summary"
  return "Open last summary: " + truncate(lastCall.title, 34)
}

// ---------------------------------------------------------------- Settings
// Helpers for SettingsPanel.qml. Kept pure/testable here per the same
// convention as the state helpers above -- SettingsPanel.qml only calls into
// these, never reimplements the formatting itself.

// Order + membership per docs/SPEC-settings-and-providers.md section 8
// ("phase 1 = voxtype + Deepgram only", which narrows section 7's five-way
// choice -- OpenAI-compatible, AssemblyAI and Soniox move to phase 2; see
// docs/ROADMAP.md and docs/phase2/). There is no local model list or
// download UI -- local just follows whatever voxtype is already set to.
// Local is the default and leads the choice.
var TRANSCRIPTION_PROVIDERS = [
  { value: "local", label: "Local" },
  { value: "deepgram", label: "Deepgram" }
]

function providerOptions() {
  return TRANSCRIPTION_PROVIDERS.slice()
}

function providerLabel(value) {
  for (var i = 0; i < TRANSCRIPTION_PROVIDERS.length; i++) {
    if (TRANSCRIPTION_PROVIDERS[i].value === value) return TRANSCRIPTION_PROVIDERS[i].label
  }
  return String(value || "")
}

// A masked secret field never shows the stored value -- only a placeholder
// describing where it comes from, per `config get --json`'s per-key shape
// {"set": bool, "source": "config"|"env"|"command"|"none"}. "config" and
// "env" both mean "there's a value in place, typing overwrites it"; only
// "command" (resolved by running another program) and "none" need their own
// wording.
function keySourcePlaceholder(secret) {
  if (!secret || typeof secret !== "object" || !secret.set) return "Not set"
  if (secret.source === "command") return "From command"
  if (secret.source === "none") return "Not set"
  return "Set"
}

// Title-cases an engine name for display -- "whisper" -> "Whisper" -- for
// the Local section's "Using voxtype: <Engine> <model>" line, built from
// `spitball local info --json`'s {"engine": "whisper", "model": "base.en"}.
function engineTitle(engine) {
  var e = String(engine || "")
  return e ? e.charAt(0).toUpperCase() + e.slice(1) : ""
}

// The Local section's status line, e.g. "Using voxtype: Whisper base.en".
// Blank when there's nothing to report yet (not installed, or no info).
function localInfoLine(info) {
  if (!info || typeof info !== "object" || !info.installed) return ""
  var parts = []
  var engine = engineTitle(info.engine)
  if (engine) parts.push(engine)
  if (info.model) parts.push(String(info.model))
  return parts.length ? "Using voxtype: " + parts.join(" ") : "Using voxtype"
}

// voxtype model slug -> a friendly display name, for the settings picker
// and the bar-widget's model-switch tooltip alike. Falls back to the raw
// slug for anything not in the table (new models voxtype adds show up
// ungainly rather than disappearing).
var MODEL_FRIENDLY_NAMES = {
  "parakeet-tdt-0.6b-v3-int8": "Parakeet v3 (int8)",
  "parakeet-tdt-0.6b-v3": "Parakeet v3",
  "parakeet-tdt-0.6b-v2-int8": "Parakeet v2 (int8)",
  "parakeet-tdt-0.6b-v2": "Parakeet v2",
  "parakeet-unified-en-0.6b": "Parakeet (unified, English)",
  "tiny": "Whisper Tiny", "tiny.en": "Whisper Tiny (English)",
  "base": "Whisper Base", "base.en": "Whisper Base (English)",
  "small": "Whisper Small", "small.en": "Whisper Small (English)",
  "medium": "Whisper Medium", "medium.en": "Whisper Medium (English)",
  "large-v3": "Whisper Large v3", "large-v3-turbo": "Whisper Large v3 Turbo"
}

// Accepts either a model slug string or a `local models --json` entry
// object (anything with a `.name`).
function modelFriendlyName(nameOrModel) {
  var n = (nameOrModel && typeof nameOrModel === "object") ? String(nameOrModel.name || "") : String(nameOrModel || "")
  if (!n) return ""
  return MODEL_FRIENDLY_NAMES[n] || n
}

// One `spitball local models --json` entry -> its picker row label, e.g.
// "Parakeet v3 (int8) — 25 European languages · 640 MB · Recommended ·
// ✓ installed". Every segment after the name is optional and only appears
// when the entry actually carries it, so a sparse backend response still
// renders something sane.
function localModelLabel(m) {
  if (!m || typeof m !== "object") return ""
  var name = String(m.name || "")
  if (!name) return ""
  var label = modelFriendlyName(m)
  var tail = []
  if (m.languages) tail.push(String(m.languages))
  if (m.size_mb !== undefined && m.size_mb !== null) tail.push(Math.round(Number(m.size_mb)) + " MB")
  if (m.recommended) tail.push("Recommended")
  if (m.installed) tail.push("✓ installed")
  return tail.length ? (label + " — " + tail.join(" · ")) : label
}

// One `local models --json` entry matching `name`, or null.
function findModel(models, name) {
  var list = Array.isArray(models) ? models : []
  for (var i = 0; i < list.length; i++) {
    if (list[i] && list[i].name === name) return list[i]
  }
  return null
}

// The settings popup's inline confirmation row text when the user picks a
// different model than the active one, e.g. "Switch to Parakeet v3 (int8)?
// Downloads about 640 MB and asks for your password once. Also changes
// Omarchy dictation."
function switchConfirmText(models, name) {
  var m = findModel(models, name)
  var label = m ? modelFriendlyName(m) : modelFriendlyName(name)
  var sizeText = (m && m.size_mb !== undefined && m.size_mb !== null)
    ? " Downloads about " + Math.round(Number(m.size_mb)) + " MB and asks for your password once."
    : " Asks for your password once."
  return "Switch to " + (label || name) + "?" + sizeText + " Also changes Omarchy dictation."
}

// Dropdown-ready {value, label} options built from `local models --json`,
// skipping any entry that somehow has no name (nothing to select or to pass
// to `spitball local set-model`).
function localModelOptions(models) {
  var list = Array.isArray(models) ? models : []
  var out = []
  for (var i = 0; i < list.length; i++) {
    var name = String((list[i] && list[i].name) || "")
    if (!name) continue
    out.push({ value: name, label: localModelLabel(list[i]) })
  }
  return out
}

// The name of whichever entry is marked active, or "" if none is (including
// a non-array/empty list).
function activeModelName(models) {
  var list = Array.isArray(models) ? models : []
  for (var i = 0; i < list.length; i++) {
    if (list[i] && list[i].active) return String(list[i].name || "")
  }
  return ""
}

// ------------------------------------------------------------ model switch
// Helpers for `$XDG_RUNTIME_DIR/spitball/model.json`, the progress file a
// background `spitball local set-model` write to (see
// spitball/providers/local.py and CONTRACT.md) -- read by both
// SettingsPanel.qml (the popup, when reopened mid-switch) and Widget.qml
// (the bar dot, while the popup is closed). Never confused with
// state.json/parseState() above -- a model switch is fully independent of
// the daemon's own call-recording state machine.

var MODEL_SWITCH_ACTIVE_STATES = ["switching-engine", "downloading", "activating", "restarting"]

// Parses model.json's text. Missing/unparsable/not-an-object all mean "no
// switch in flight or ever recorded" -- returns null, never throws.
function parseModelState(text) {
  try {
    var s = JSON.parse(String(text || ""))
    return (s && typeof s === "object") ? s : null
  } catch (e) {
    return null
  }
}

function modelSwitchActive(ms) {
  return !!ms && MODEL_SWITCH_ACTIVE_STATES.indexOf(ms.state) !== -1
}

function modelSwitchFailed(ms) {
  return !!ms && ms.state === "error"
}

// 0-100, or -1 when there's no usable byte count yet (engine-switch step,
// or a download that hasn't reported its first event).
function modelSwitchPercent(ms) {
  var total = ms ? Number(ms.total_bytes) || 0 : 0
  if (total <= 0) return -1
  var done = ms ? Number(ms.done_bytes) || 0 : 0
  return Math.max(0, Math.min(100, Math.round(done / total * 100)))
}

// The one line shown for a model switch, wherever it's shown: the settings
// popup (as plain status text above a progress bar) and the bar widget's
// tooltip (e.g. "Downloading Parakeet v3 (int8)… 42%"). `ms` may be null.
function modelSwitchStatusText(ms) {
  if (!ms) return ""
  var label = modelFriendlyName(ms.name)
  switch (ms.state) {
    case "error":
      return "Model switch failed: " + (ms.message || "unknown error")
    case "switching-engine":
      return ms.message || "Switching voxtype's engine…"
    case "downloading": {
      var pct = modelSwitchPercent(ms)
      return "Downloading " + (label || "model") + "…" + (pct >= 0 ? (" " + pct + "%") : "")
    }
    case "activating":
      return "Activating " + (label || "model") + "…"
    case "restarting":
      return ms.message || "Restarting voxtype…"
    case "terminal":
      return ms.message || "Opened a terminal to finish the switch."
    case "done":
      return ms.message || ("Switched to " + (label || "model"))
    default:
      return ms.message || ""
  }
}

// ---------------------------------------------------------------- language

var LANGUAGE_CHOICES = [
  { value: "en", label: "English" },
  { value: "auto", label: "Auto-detect" },
  { value: "other", label: "Other…" }
]

function languageOptions() {
  return LANGUAGE_CHOICES.slice()
}

// Which of the three chips a stored `language` config value corresponds to.
// Empty/missing reads as English (the default); "auto" is its own chip;
// anything else (an ISO code the user typed) is "other".
function languageChoiceFor(code) {
  var c = String(code || "en")
  if (c === "" || c === "en") return "en"
  if (c === "auto") return "auto"
  return "other"
}

// ---------------------------------------------------------------- live transcript
// Helpers for LivePopup.qml and $XDG_RUNTIME_DIR/spitball/live.json (see
// spitball/live.py and CONTRACT.md). Parallels parseState() above: always
// returns a fully-populated object so callers never null-check every field.

var LIVE_STATUSES = ["listening", "catching-up", "unavailable", "stopped"]

function emptyLiveState() {
  return { call_id: "", started_at: 0, status: "", message: "", utterances: [] }
}

// Missing/unparseable/non-object text all read as the empty state above --
// same "never throws, always returns something sane" contract as
// parseState(). An unrecognized status string is blanked rather than kept,
// so liveStatusText() below falls through to its "" default instead of
// inventing a status line for something the widget doesn't understand.
function parseLiveState(text) {
  var base = emptyLiveState()
  try {
    var s = JSON.parse(String(text || ""))
    if (!s || typeof s !== "object") return base
    for (var k in base) if (s[k] === undefined) s[k] = base[k]
    if (LIVE_STATUSES.indexOf(s.status) === -1) s.status = ""
    if (typeof s.message !== "string") s.message = ""
    if (!Array.isArray(s.utterances)) s.utterances = []
    return s
  } catch (e) {
    return base
  }
}

// The popup's status line, per docs/SPEC-live-transcript.md: "Listening…"
// while it's keeping up, a distinct line while it's behind, or the reason
// it isn't available at all (live.message carries that reason, e.g. "Live
// transcript needs voxtype"). Note: the popup's OTHER text mode --
// "Recording saved; transcribing…" once the call itself has stopped -- is
// driven by the daemon's own state (state.json leaving "recording"), not by
// this status, so it isn't handled here; see LivePopup.qml.
function liveStatusText(live) {
  if (!live) return ""
  switch (live.status) {
    case "listening": return "Listening…"
    case "catching-up": return "Catching up…"
    case "unavailable": return live.message || "Live transcript unavailable"
    case "stopped": return "Live transcript stopped."
    default: return ""
  }
}

// One bubble per closed segment (each one its own transcribed chunk -- live
// segments arrive incrementally, so unlike build_transcript() there's no
// consecutive-line merging here). `showLabel` is true only when the
// previous bubble was a different channel/speaker, per the spec ("Speaker
// label above a bubble only when the speaker changes").
function liveBubbles(utterances) {
  var list = (Array.isArray(utterances) ? utterances.slice() : [])
    .filter(function(u) { return u && typeof u === "object" })
  list.sort(function(a, b) { return (Number(a.start) || 0) - (Number(b.start) || 0) })
  var out = []
  var prevKey = null
  for (var i = 0; i < list.length; i++) {
    var u = list[i]
    var key = String(u.channel) + ":" + String(u.speaker || 0)
    out.push({
      channel: u.channel, speaker: u.speaker || 0,
      start: Number(u.start) || 0, end: Number(u.end) || 0,
      transcript: String(u.transcript || ""), failed: !!u.failed,
      partial: !!u.partial,
      showLabel: key !== prevKey
    })
    prevKey = key
  }
  return out
}

// channel 0 (mine) sits on the right, per spec; anything else is the far side.
function liveBubbleSide(channel) {
  return channel === 0 ? "right" : "left"
}

function liveSpeakerLabel(channel, myName) {
  return channel === 0 ? (myName || "Me") : "Them"
}

// "m:ss" from a bubble's own start offset (seconds into the call) -- same
// digit shape as elapsedLabel() above, but for a plain offset rather than
// started_at/now.
function liveTimestampLabel(seconds) {
  var secs = Math.max(0, Math.floor(Number(seconds) || 0))
  var m = Math.floor(secs / 60)
  var s = secs % 60
  return m + ":" + pad2(s)
}

// True when newly-arrived content should auto-scroll the view to the
// bottom -- only when the viewport was already at (or within `thresholdPx`
// of) the bottom beforehand. Otherwise the caller keeps the scroll position
// and shows the "New messages ↓" pill instead (per spec). Takes a
// Flickable's own contentY/height/contentHeight so QML can call this
// directly with its own geometry.
function liveShouldAutoScroll(contentY, viewHeight, contentHeight, thresholdPx) {
  var threshold = thresholdPx === undefined ? 24 : thresholdPx
  var distanceFromBottom = contentHeight - (contentY + viewHeight)
  return distanceFromBottom <= threshold
}

// ---------------------------------------------------------------- settings overlay
// Helpers for SettingsWindow.qml / settings/SettingsCard.qml (docs/SPEC-v2.md
// section 1). The section list is the one place the overlay's navigation is
// defined: SettingsNav renders it, the digit keys index into it, and the page
// Loader resolves `page` relative to settings/. Adding a section later means
// one entry here plus one settings/<Page>.qml file.
//
// `placeholder: true` marks a page that ships as "Coming in this release"
// (Audio, Speakers, Calendar -- filled by later phases); the nav shows it
// with a "Soon" badge until the flag is dropped.

var SETTINGS_SECTIONS = [
  { id: "general",       label: "General",       page: "GeneralPage.qml" },
  { id: "recording",     label: "Recording",     page: "RecordingPage.qml" },
  { id: "transcription", label: "Transcription", page: "TranscriptionPage.qml" },
  { id: "live",          label: "Live",          page: "LivePage.qml" },
  { id: "audio",         label: "Audio",         page: "AudioPage.qml", placeholder: true },
  { id: "speakers",      label: "Speakers",      page: "SpeakersPage.qml", placeholder: true },
  { id: "summary",       label: "Summary",       page: "SummaryPage.qml" },
  { id: "calendar",      label: "Calendar",      page: "CalendarPage.qml", placeholder: true },
  { id: "storage",       label: "Storage",       page: "StoragePage.qml" },
  { id: "about",         label: "About",         page: "AboutPage.qml" }
]

// A fresh copy each call (one new object per entry), so a caller can't
// mutate the source list.
function settingsSections() {
  var out = []
  for (var i = 0; i < SETTINGS_SECTIONS.length; i++) {
    var s = SETTINGS_SECTIONS[i]
    out.push({ id: s.id, label: s.label, page: s.page, placeholder: !!s.placeholder })
  }
  return out
}

function settingsDefaultSection() {
  return SETTINGS_SECTIONS[0].id
}

// Index of a section id, or -1 for anything unknown (including ""/null).
function settingsSectionIndex(id) {
  var key = String(id || "")
  for (var i = 0; i < SETTINGS_SECTIONS.length; i++) {
    if (SETTINGS_SECTIONS[i].id === key) return i
  }
  return -1
}

// The entry at `index`, or null when out of range / not a number.
function settingsSectionAt(index) {
  var i = Number(index)
  if (!isFinite(i) || i < 0 || i >= SETTINGS_SECTIONS.length) return null
  var s = SETTINGS_SECTIONS[Math.floor(i)]
  return { id: s.id, label: s.label, page: s.page, placeholder: !!s.placeholder }
}

// Where j/k (or Up/Down) land from `index`: clamped to the list, never
// wrapping, so holding j parks on the last section instead of cycling. A
// bad starting index reads as the first section.
function settingsNavStep(index, delta) {
  var n = SETTINGS_SECTIONS.length
  var i = Number(index)
  if (!isFinite(i) || i < 0 || i >= n) i = 0
  var d = Number(delta) || 0
  return Math.max(0, Math.min(n - 1, Math.floor(i) + d))
}

// The section index a single typed character selects ("1" -> 0 ... "9" ->
// 8), or -1 when the character isn't a digit in range. "0" is never a jump
// key -- there's no tenth slot to reach, and it's easy to hit by accident.
function settingsSectionForKey(text) {
  var t = String(text || "")
  if (t.length !== 1 || t < "1" || t > "9") return -1
  var i = Number(t) - 1
  return i < SETTINGS_SECTIONS.length ? i : -1
}

// The small trailing badge on a nav row, or "" for none: "Set up" on
// Transcription whenever the bar's own gear would show (a setup_needed reason
// while idle, or a failed model switch), "Soon" on a placeholder page.
function settingsSectionBadge(id, st, modelSwitch) {
  var key = String(id || "")
  if (key === "transcription") {
    if ((st && needsSetup(st)) || modelSwitchFailed(modelSwitch)) return "Set up"
    return ""
  }
  var i = settingsSectionIndex(key)
  if (i >= 0 && SETTINGS_SECTIONS[i].placeholder) return "Soon"
  return ""
}

// ------------------------------------------------------------ call_apps
// `call_apps` is a {process-name: "Display name"} map (config.DEFAULT_CALL_APPS).
// The Recording page edits it as a whole value -- `spitball config set
// call_apps '<json>'` parses a JSON object -- so these build the next map
// rather than mutating the loaded one.

function callAppsList(map) {
  if (!map || typeof map !== "object" || Array.isArray(map)) return []
  var keys = Object.keys(map).sort()
  var out = []
  for (var i = 0; i < keys.length; i++) {
    out.push({ key: keys[i], label: String(map[keys[i]] || keys[i]) })
  }
  return out
}

// Display name for a typed process name: first letter upper-cased, the rest
// kept as typed ("zoom" -> "Zoom", "WhatsApp" -> "WhatsApp").
function callAppLabel(name) {
  var n = String(name || "").trim()
  return n ? n.charAt(0).toUpperCase() + n.slice(1) : ""
}

// A fresh {key: label} map in sorted key order (so the JSON written to
// config.json, and every chip row, reads the same regardless of the order
// apps were added in).
function callAppsSorted(map) {
  var next = {}
  var list = callAppsList(map)
  for (var i = 0; i < list.length; i++) next[list[i].key] = list[i].label
  return next
}

// A copy of `map` with `name` added under its lower-cased key. Blank input,
// or a name already present, returns an unchanged copy.
function callAppsAdd(map, name) {
  var next = callAppsSorted(map)
  var key = String(name || "").trim().toLowerCase()
  if (key && next[key] === undefined) next[key] = callAppLabel(name)
  return callAppsSorted(next)
}

function callAppsRemove(map, key) {
  var next = callAppsSorted(map)
  delete next[String(key || "")]
  return next
}

// The argv value for `spitball config set call_apps ...` -- compact JSON
// the CLI's _parse_value() reads back as an object.
function callAppsSerialize(map) {
  return JSON.stringify(callAppsSorted(map))
}

// ------------------------------------------------------------ opus bitrate
// The Recording page's bitrate dropdown. Any value already in config that
// isn't one of these still shows (as itself) rather than snapping the
// dropdown to a wrong choice -- `opus_bitrate` is passed straight to ffmpeg,
// so a hand-edited "40k" is perfectly valid.

var OPUS_BITRATES = ["16k", "24k", "32k", "48k", "64k", "96k", "128k"]

function opusBitrateOptions(current) {
  var out = []
  var cur = String(current || "")
  var seen = false
  for (var i = 0; i < OPUS_BITRATES.length; i++) {
    var v = OPUS_BITRATES[i]
    if (v === cur) seen = true
    out.push({ value: v, label: v === "32k" ? v + " (default)" : v })
  }
  if (cur && !seen) out.push({ value: cur, label: cur })
  return out
}

// ------------------------------------------------------------ live engine
// One line for `spitball live status --json`'s {installed, model, fast} --
// the same wording the CLI prints without --json. Null/garbage reads blank.
function liveEngineStatusLine(status) {
  if (!status || typeof status !== "object") return ""
  if (status.fast) return "Fast engine: on" + (status.model ? " (" + status.model + ")" : "")
  if (!status.installed) return "Fast engine: not installed"
  return "Fast engine: installed, but voxtype isn't on a Parakeet model"
}

// ------------------------------------------------------------ about / status
// The daemon line in the overlay header and on the About page.
function daemonStateLabel(st) {
  var s = st && st.state ? String(st.state) : "offline"
  switch (s) {
    case "offline": return "Daemon: not running"
    case "idle": return "Daemon: idle"
    case "detected": return "Daemon: call detected" + (st.app ? " in " + st.app : "")
    case "recording": return "Daemon: recording" + (st.app ? " " + st.app : "")
    case "processing": return "Daemon: processing"
    case "error": return "Daemon: error" + (st.message ? " — " + st.message : "")
    default: return "Daemon: " + s
  }
}

// `status --json`'s `present` list (call apps holding the mic right now),
// for the Recording page's troubleshooting read-out.
function presentAppsLine(present) {
  var list = Array.isArray(present) ? present.filter(function(p) { return !!p }) : []
  if (!list.length) return "No call app is using the microphone right now."
  return "Using the microphone now: " + list.join(", ")
}
