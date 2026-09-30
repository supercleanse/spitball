import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import "../Model.js" as Model

// The one object every settings page shares: the loaded settings dict and
// every CLI round trip behind the overlay (docs/SPEC-v2.md section 1).
// Pages are thin views over this; nothing in settings/*Page.qml spawns a
// process or parses JSON itself.
//
// Every value on screen comes from `spitball config get --json`, loaded when
// `active` turns on; every change is written back with `config set` /
// `config set-secret` as it happens. Any CLI failure (nonzero exit,
// unparsable JSON, missing binary) degrades to a visible message and
// disabled-feeling controls -- this file never throws on a CLI failure;
// every Process result is checked.
//
// requestClose(): the overlay must close BEFORE this store launches
// anything outside the shell -- a portal folder picker, the voxtype
// installer, the live-engine installer, or (via `spitball local set-model`)
// a graphical pkexec/polkit prompt or a fallback terminal. All of those are
// ordinary top-level windows; the overlay is a layer-shell surface with
// exclusive keyboard focus that Hyprland stacks ABOVE them, which is exactly
// what made the original "Upgrade to Parakeet" button invisible and
// unusable: the terminal and its sudo prompt opened BEHIND the panel, which
// also still held keyboard focus.
// requestReopen(): fired once a *synchronous* external action with a clear
// completion point (the folder picker) is done, so the overlay reappears
// with the fresh result already loaded. Installers and model switches never
// reopen this way -- they can run for minutes, and their progress is meant
// to be watched on the bar widget until the user reopens Settings by hand.
QtObject {
  id: root

  // Absolute path to bin/spitball, exactly as Widget.qml resolves it.
  required property string cliPath
  // $XDG_RUNTIME_DIR/spitball -- watched for model.json, the progress file a
  // background `spitball local set-model` writes (CONTRACT.md).
  required property string runtimeDir
  // Bind to the overlay's `open`. Loads settings the moment it becomes
  // visible; stops the model-switch watch when it closes.
  property bool active: false

  // Theme, handed down from Widget.qml (bar-provided colors/font when a
  // bar is present, plain theme tokens otherwise).
  property color foreground: Color.popups.text
  property string fontFamily: Style.font.family

  // The daemon's parsed state.json (Model.parseState shape) -- Widget.qml's
  // `st`, passed in rather than re-read so the overlay and the bar agree.
  property var st: Model.emptyState()

  signal requestClose()
  signal requestReopen()

  // Both: the overlay flips `active` false -> true on open, but a host that
  // creates the store already active (tests/offscreen) fires the change
  // during construction, before `cliPath`'s binding has been evaluated.
  // scheduleLoad() defers past construction and collapses the two into one.
  onActiveChanged: if (active) root.scheduleLoad()
  Component.onCompleted: if (root.active) root.scheduleLoad()

  property bool _loadScheduled: false
  function scheduleLoad() {
    if (root._loadScheduled) return
    root._loadScheduled = true
    Qt.callLater(function() { root._loadScheduled = false; root.load() })
  }

  // ------------------------------------------------------------ CLI plumbing

  // One-shot process runner: args go straight to
  // `/usr/bin/python3 -I <cliPath> <args...>`, matching how Widget.qml and
  // CONTRACT.md's CLI table both invoke the plugin's own CLI. `onDone(code,
  // stdoutText, stderrText)` always fires, even if the process never
  // manages to start (Qt reports that as a nonzero exit here too).
  property Component _cliRunner: Component {
    Process {
      id: proc
      property var onDone: null
      stdout: StdioCollector { id: out; waitForEnd: true }
      stderr: StdioCollector { id: err; waitForEnd: true }
      onExited: function(code) {
        var cb = proc.onDone
        var so = out.text
        var se = err.text
        proc.destroy()
        if (cb) cb(code, so, se)
      }
    }
  }

  function runCli(args, onDone) {
    var full = ["/usr/bin/python3", "-I", root.cliPath].concat(args)
    var proc = root._cliRunner.createObject(root, { command: full, onDone: onDone })
    if (!proc) { if (onDone) onDone(-1, "", "failed to start"); return }
    proc.running = true
  }

  // Same, but parses stdout as JSON. `cb(ok, data, errorText)` -- `ok` is
  // false on a nonzero exit OR unparsable stdout, so callers never need to
  // JSON.parse anything themselves.
  function runCliJson(args, cb) {
    root.runCli(args, function(code, stdoutText, stderrText) {
      if (code !== 0) { cb(false, null, stderrText || "exit " + code); return }
      try {
        cb(true, JSON.parse(stdoutText), "")
      } catch (e) {
        cb(false, null, "bad JSON from CLI")
      }
    })
  }

  // Same shape as runCli, but for a plain command -- not prefixed with
  // `/usr/bin/python3 -I <cliPath>`. Used for the small "is this binary on
  // PATH" checks and the read-only pactl device lookups.
  function runRaw(command, onDone) {
    var proc = root._cliRunner.createObject(root, { command: command, onDone: onDone })
    if (!proc) { if (onDone) onDone(-1, "", "failed to start"); return }
    proc.running = true
  }

  // ------------------------------------------------------------ loaded state

  property var settings: ({})
  property bool loading: false
  property bool loadFailed: false

  // `spitball local info --json` -- whether voxtype is installed and what
  // it's currently set to. `spitball local models --json` -- the picker
  // list (name, engine, installed, size_mb, languages, recommended, active).
  property var localInfo: ({ installed: false, engine: "", model: "", message: "", can_upgrade_parakeet: false })
  property bool localInfoFailed: false
  property var localModels: []
  property bool localModelsFailed: false
  property var summaryModels: []

  // `spitball live status --json` -- {installed, venv, model, fast}.
  property var liveStatus: null
  property bool liveStatusFailed: false

  // `spitball status --json` -- only `present` (call apps holding the mic
  // right now) is used here, for the Recording page's read-out. `null`
  // until the first successful read; the daemon being down reads as [].
  property var presentApps: null

  // `pactl get-default-source` / `get-default-sink` -- read-only, for the
  // Audio page's "what Spitball would record" lines. Empty until read.
  property string audioSource: ""
  property string audioSink: ""

  // Plugin metadata for the About page: manifest.json's version, and the
  // plugin directory itself (resolved from this file's location).
  readonly property string pluginDir: Qt.resolvedUrl("..").toString().replace(/^file:\/\//, "").replace(/\/$/, "")
  property string version: ""
  property FileView manifestFile: FileView {
    path: root.pluginDir + "/manifest.json"
    printErrors: false
    onLoaded: {
      try { root.version = String(JSON.parse(text()).version || "") } catch (e) { root.version = "" }
    }
    onLoadFailed: function(error) { root.version = "" }
  }

  function value(key, fallback) {
    var v = root.settings ? root.settings[key] : undefined
    return v === undefined || v === null ? fallback : v
  }

  function secretInfo(key) {
    var v = root.settings ? root.settings[key] : undefined
    return (v && typeof v === "object") ? v : { set: false, source: "none" }
  }

  readonly property string provider: String(root.value("transcription_provider", "local"))
  readonly property string languageChoice: Model.languageChoiceFor(root.value("language", "en"))

  // The model picker's pending selection -- synced to whatever's active
  // whenever a fresh model list arrives, unless the user has since picked
  // something else with the dropdown (pendingModelDirty).
  property string pendingModelName: ""
  property bool pendingModelDirty: false

  // model.json, watched live (not polled) so a switch kicked off from THIS
  // overlay, or from a previous session, shows up the moment it's reopened
  // -- see Model.js's modelSwitch* helpers and Widget.qml's own watch of the
  // same file for the bar-widget side of this.
  readonly property string modelStateFilePath: root.runtimeDir + "/model.json"
  property var modelSwitch: null
  readonly property bool modelSwitchBusy: Model.modelSwitchActive(root.modelSwitch)

  property FileView modelStateFile: FileView {
    path: root.modelStateFilePath
    watchChanges: root.active
    printErrors: false
    onFileChanged: reload()
    onLoaded: root.modelSwitch = Model.parseModelState(text())
    onLoadFailed: function(error) { root.modelSwitch = null }
  }

  function loadLocalInfo() {
    root.runCliJson(["local", "info", "--json"], function(ok, data) {
      root.localInfoFailed = !ok
      if (ok && data && typeof data === "object") root.localInfo = data
    })
  }

  function loadLocalModels() {
    root.runCliJson(["local", "models", "--json"], function(ok, data) {
      root.localModelsFailed = !ok
      root.localModels = (ok && Array.isArray(data)) ? data : []
      if (!root.pendingModelDirty) root.pendingModelName = Model.activeModelName(root.localModels)
    })
  }

  function loadLiveStatus() {
    root.runCliJson(["live", "status", "--json"], function(ok, data) {
      root.liveStatusFailed = !ok
      root.liveStatus = (ok && data && typeof data === "object") ? data : null
    })
  }

  function loadPresentApps() {
    root.runCliJson(["status", "--json"], function(ok, data) {
      root.presentApps = (ok && data && Array.isArray(data.present)) ? data.present : []
    })
  }

  function loadAudioDevices() {
    root.runRaw(["pactl", "get-default-source"], function(code, stdoutText) {
      root.audioSource = code === 0 ? String(stdoutText || "").trim() : ""
    })
    root.runRaw(["pactl", "get-default-sink"], function(code, stdoutText) {
      root.audioSink = code === 0 ? String(stdoutText || "").trim() : ""
    })
  }

  // Everything the overlay shows, fetched in parallel on open. Each page
  // also refreshes its own slice when it appears (see the pages' Component.
  // onCompleted), so a page opened minutes later isn't stale.
  // Each load() gets a serial so a slow (or failed) earlier read can't
  // land after a newer one and overwrite its result or its failure flag.
  property int _loadSerial: 0

  function load() {
    var serial = ++root._loadSerial
    root.loading = true
    root.loadFailed = false
    root.runCliJson(["config", "get", "--json"], function(ok, data) {
      if (serial !== root._loadSerial) return
      root.loading = false
      if (!ok || !data || typeof data !== "object") { root.loadFailed = true; return }
      root.settings = data
    })
    root.loadLocalInfo()
    root.loadLocalModels()
    root.loadLiveStatus()
    root.runCliJson(["check", "summary", "--json"], function(ok, data) {
      root.summaryModels = (ok && data && Array.isArray(data.models)) ? data.models : []
    })
  }

  // ------------------------------------------------------------ saving

  // Transient "couldn't save that one" flag, keyed by settings key so only
  // the row that actually failed shows it.
  property string errorKey: ""
  property string errorText: ""

  property Timer errorClearTimer: Timer {
    interval: 4000
    onTriggered: { root.errorKey = ""; root.errorText = "" }
  }

  function flashError(key, message) {
    root.errorKey = key
    root.errorText = message || "Couldn't save"
    root.errorClearTimer.restart()
  }

  // Optimistic: the control already shows the new value the instant it's
  // set; a failed CLI round-trip surfaces as flashError rather than
  // snapping the control back (which reads as the click "not registering").
  // Objects (call_apps) go over as JSON, which `config set` parses back.
  function setKey(key, rawValue) {
    var next = Object.assign({}, root.settings)
    next[key] = rawValue
    root.settings = next
    var argValue
    if (typeof rawValue === "boolean") argValue = rawValue ? "true" : "false"
    else if (rawValue && typeof rawValue === "object") argValue = JSON.stringify(rawValue)
    else argValue = String(rawValue)
    root.runCli(["config", "set", key, argValue], function(code, stdoutText, stderrText) {
      if (code !== 0) root.flashError(key, stderrText || "Couldn't save")
    })
  }

  // Back to the default: `config unset`, then a reload so the effective
  // value (the default) lands in `settings` the same way everything else does.
  function unsetKey(key) {
    root.runCli(["config", "unset", key], function(code, stdoutText, stderrText) {
      if (code !== 0) { root.flashError(key, stderrText || "Couldn't reset"); return }
      root.load()
    })
  }

  property Component _secretRunner: Component {
    Process {
      id: sproc
      property string key: ""
      property string secret: ""
      stdinEnabled: true
      stdout: StdioCollector { waitForEnd: true }
      stderr: StdioCollector { id: serr; waitForEnd: true }
      onStarted: { write(sproc.secret + "\n"); sproc.secret = "" }
      onExited: function(code) {
        var k = sproc.key
        var msg = serr.text
        sproc.destroy()
        if (code === 0) root.load()  // refresh so the placeholder reflects the new source
        else root.flashError(k, msg || "Couldn't save")
      }
    }
  }

  // The plaintext goes to `config set-secret` over stdin -- never argv, so
  // it never lands in `ps` output or shell history.
  function setSecret(key, plaintext) {
    var proc = root._secretRunner.createObject(root, { key: key, secret: plaintext })
    if (!proc) { root.flashError(key, "Couldn't save"); return }
    proc.command = ["/usr/bin/python3", "-I", root.cliPath, "config", "set-secret", key]
    proc.running = true
  }

  // ------------------------------------------------------------ folder picker

  property bool callsDirEditable: false
  property bool notesDirEditable: false

  // Closes the overlay before the portal folder-chooser dialog opens
  // (layering rule -- see the file comment), then reopens as soon as the
  // picker resolves (picked, canceled, or "no picker available" all count --
  // the dialog itself is unambiguously gone by the time this callback fires).
  function pickFolder(kind) {
    var title = kind === "calls" ? "Choose where to save calls" : "Choose the notes copy folder"
    var key = kind === "calls" ? "calls_dir" : "export_dir"
    root.requestClose()
    root.runCli(["pick-folder", "--title", title], function(code, stdoutText) {
      if (code === 0) {
        var path = String(stdoutText || "").trim()
        if (path) root.setKey(key, path)
      } else if (code === 2) {
        if (kind === "calls") root.callsDirEditable = true
        else root.notesDirEditable = true
      }
      // code === 1: user canceled -- nothing to do.
      root.requestReopen()
    })
  }

  // ------------------------------------------------------------ test buttons

  property bool deepgramTesting: false
  property string deepgramTestMsg: ""
  property bool summaryTesting: false
  property string summaryTestMsg: ""
  property bool calendarTesting: false
  property string calendarTestMsg: ""

  function checkResultText(ok, data) {
    if (!ok || !data) return "✗ Couldn't reach spitball"
    return (data.ok ? "✓ " : "✗ ") + (data.message || (data.ok ? "OK" : "Failed"))
  }

  function testTranscription(providerName) {
    root.deepgramTesting = true
    root.deepgramTestMsg = ""
    root.runCliJson(["check", "transcription", "--provider", providerName, "--json"], function(ok, data) {
      root.deepgramTesting = false
      root.deepgramTestMsg = root.checkResultText(ok, data)
    })
  }

  function testSummary() {
    root.summaryTesting = true
    root.summaryTestMsg = ""
    root.runCliJson(["check", "summary", "--json"], function(ok, data) {
      root.summaryTesting = false
      root.summaryTestMsg = root.checkResultText(ok, data)
      if (ok && data && Array.isArray(data.models) && data.models.length) root.summaryModels = data.models
    })
  }

  // `spitball calendar test --json`: fetches (or reuses the cached) feed, or
  // runs calendar_command, and reports the match for a call starting now.
  // A real network call for the ICS source; nothing opens a window, so no
  // layering dance is needed.
  function testCalendar() {
    root.calendarTesting = true
    root.calendarTestMsg = ""
    root.runCliJson(["calendar", "test", "--json"], function(ok, data) {
      root.calendarTesting = false
      root.calendarTestMsg = Model.calendarTestText(ok, data)
    })
  }

  // ------------------------------------------------------------ local (voxtype)

  // Voxtype's own installer, for the "not installed" case only -- the model
  // picker covers everything once it IS installed. Fire-and-forget (there's
  // no completion signal for a detached launch), so this closes the overlay
  // and leaves it closed -- the installer's own window/terminal must not
  // fight our layer-shell surface for stacking, and unlike pickFolder()
  // there's no clear moment to reopen at. The poll below still runs so
  // `local info` is fresh the moment the user reopens Settings.
  function installVoxtype() {
    root.requestClose()
    root.runRaw(["/bin/sh", "-c", "command -v omarchy-voxtype-install"], function(code) {
      if (code === 0) Quickshell.execDetached(["omarchy-voxtype-install"])
      else Quickshell.execDetached(["omarchy-launch-floating-terminal-with-presentation", "voxtype setup"])
      root.beginLocalPoll()
    })
  }

  // The model dropdown's inline confirmation row ([Switch] [Cancel]) lands
  // here. `spitball local set-model <name>` starts the switch in the
  // background and returns immediately -- but it may still need to show a
  // graphical pkexec prompt, or fall back to a floating terminal, so this
  // closes the overlay first regardless (layering rule). It does NOT
  // reopen: the switch can run for minutes, and its progress is meant to be
  // watched on the bar widget (Widget.qml's showModelSwitch) until the user
  // reopens Settings by hand, where modelSwitch (above) already reflects it.
  function confirmSwitchModel() {
    var name = root.pendingModelName
    root.pendingModelDirty = false  // let the poll (and model.json) re-sync the picker to reality
    root.requestClose()
    root.runCli(["local", "set-model", name], function() {
      root.beginLocalPoll()
    })
  }

  // Discards the pending selection, back to whatever's actually active --
  // no CLI call, nothing external, nothing to close for.
  function cancelSwitchModel() {
    root.pendingModelDirty = false
    root.pendingModelName = Model.activeModelName(root.localModels)
  }

  // Re-reads `local info` + `local models` every 3s for up to ~5 minutes --
  // long enough to cover an interactive floating-terminal install/model
  // switch (sudo prompt + download) without polling forever.
  property int localPollTicks: 0
  readonly property int localPollMaxTicks: 100  // 100 * 3s = 5 minutes

  function beginLocalPoll() {
    root.localPollTicks = 0
    root.localPollTimer.restart()
  }

  property Timer localPollTimer: Timer {
    interval: 3000
    repeat: true
    onTriggered: {
      root.localPollTicks += 1
      root.loadLocalInfo()
      root.loadLocalModels()
      if (root.localPollTicks >= root.localPollMaxTicks) root.localPollTimer.stop()
    }
  }

  // ------------------------------------------------------------ live engine

  // `spitball live setup` installs a venv with pip -- a minute or more, with
  // output worth seeing -- so it runs in Omarchy's floating terminal, the
  // same fallback installVoxtype() uses. Same layering rule: close first,
  // don't reopen, poll `live status` so the Live page is fresh on return.
  function installLiveEngine() {
    root.requestClose()
    Quickshell.execDetached(["omarchy-launch-floating-terminal-with-presentation",
                             "/usr/bin/python3 -I " + root.cliPath + " live setup"])
    root.beginLivePoll()
  }

  property int livePollTicks: 0

  function beginLivePoll() {
    root.livePollTicks = 0
    root.livePollTimer.restart()
  }

  property Timer livePollTimer: Timer {
    interval: 3000
    repeat: true
    onTriggered: {
      root.livePollTicks += 1
      root.loadLiveStatus()
      if (root.livePollTicks >= root.localPollMaxTicks) root.livePollTimer.stop()
    }
  }

  // ------------------------------------------------------------ misc actions

  // `omarchy-shell supercleanse.spitball restart` -- the service entry's
  // own IPC (SpitballService.qml), the documented way to restart the daemon.
  function restartDaemon() {
    Quickshell.execDetached(["omarchy-shell", "supercleanse.spitball", "restart"])
  }

  function openCallsFolder() {
    root.runCli(["open-folder"], function() {})
  }

  function openUrl(url) {
    Quickshell.execDetached(["xdg-open", String(url)])
  }
}
