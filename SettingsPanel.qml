pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

// Spitball settings panel content -- meant to be embedded inside a
// KeyboardPanel's content item (see Widget.qml). Owns nothing about the
// popup chrome/positioning; just the form and the CLI round-trips per
// docs/SPEC-settings-and-providers.md section 4, as narrowed by section 8
// ("phase 1 = voxtype + Deepgram only"): Local (voxtype) is the default
// provider, Deepgram is the other phase-1 choice. OpenAI-compatible,
// AssemblyAI and Soniox are phase 2 -- see docs/ROADMAP.md and the
// preserved draft under docs/phase2/.
//
// Every value on screen comes from `spitball config get --json`, loaded on
// open; every change is written back with `config set`/`config set-secret`
// as it happens. The backend (spitball/*.py) may not have these commands
// yet, or the daemon may be down -- any failure (nonzero exit, unparsable
// JSON, missing binary) degrades to a visible "Couldn't load settings" /
// "Couldn't save" message and disabled-feeling (but never crashing) controls.
// This file never throws on a CLI failure; every Process result is checked.
//
// Root is a plain Item (not a Column) because the content can run taller
// than the popup's screen-fitted card height: a Flickable viewport fills
// whatever height the caller lays this out at, with the real form living in
// an inner Column (`content`) that can be taller than that viewport.
Item {
  id: root

  property color foreground: Color.popups.text
  property string fontFamily: Style.font.family
  // Absolute path to bin/spitball, exactly as Widget.qml resolves it.
  required property string cliPath
  // $XDG_RUNTIME_DIR/spitball -- watched for model.json, the progress file a
  // background `spitball local set-model` writes (see
  // spitball/providers/local.py, CONTRACT.md).
  required property string runtimeDir
  // Bind to the owning KeyboardPanel's `open`. Loads settings the moment the
  // panel becomes visible; stops the model-switch watch when it closes.
  property bool active: false

  // The full form's natural height, unclamped -- Widget.qml reads this to
  // size the popup card (via KeyboardPanel.fittedContentHeight), which then
  // hands this Item back whatever height actually fit on screen.
  // The pinned header plus the whole form, so the popup can size to fit both.
  readonly property real contentHeight: settingsHeader.height + Style.space(10) + content.implicitHeight

  // requestClose(): the popup must close BEFORE this file launches anything
  // outside the shell -- a portal folder picker, the voxtype installer, or
  // (via `spitball local set-model`) a graphical pkexec/polkit prompt or a
  // fallback terminal. All of those are ordinary top-level windows/dialogs;
  // Spitball's own popup is a layer-shell surface Hyprland stacks ABOVE
  // them, which is exactly what made the original bug (the old "Upgrade to
  // Parakeet" button) invisible and unusable: the terminal and its sudo
  // prompt opened BEHIND this panel, which also still held keyboard focus.
  // requestReopen(): fired once a *synchronous* external action with a clear
  // completion point (the folder picker, the voxtype install helper) is
  // done, so the panel reappears with the fresh result already loaded. A
  // model switch never reopens this way -- it can run for minutes, and its
  // progress is meant to be watched on the bar widget instead (see
  // Widget.qml's showModelSwitch) until the user reopens Settings by hand.
  signal requestClose()
  signal requestReopen()

  onActiveChanged: if (active) root.load()

  // Scrolls to the Transcription section, e.g. when opened from the bar's
  // "needs setup" gear (setup_needed reports something like "Install
  // dictation (voxtype) or pick a cloud service", resolved under
  // Transcription -> Local). Deferred a frame past `active` turning on so
  // the Flickable/content have actually been laid out.
  property bool _pendingScrollToTranscription: false
  function scrollToTranscription(defer) {
    if (defer === false) {
      flick.contentY = Math.max(0, Math.min(Math.max(0, flick.contentHeight - flick.height), transcriptionHeader.y))
      return
    }
    root._pendingScrollToTranscription = true
    scrollDeferTimer.restart()
  }

  Timer {
    id: scrollDeferTimer
    interval: 32
    onTriggered: {
      if (root._pendingScrollToTranscription) {
        root._pendingScrollToTranscription = false
        root.scrollToTranscription(false)
      }
    }
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
  // PATH" checks ahead of the omarchy-voxtype-* helper scripts, which live
  // outside the spitball CLI entirely.
  function runRaw(command, onDone) {
    var proc = root._cliRunner.createObject(root, { command: command, onDone: onDone })
    if (!proc) { if (onDone) onDone(-1, "", "failed to start"); return }
    proc.running = true
  }

  // ------------------------------------------------------------ loaded state

  property var settings: ({})
  property bool loading: false
  property bool loadFailed: false

  // `spitball local info --json` -- whether voxtype is installed, what it's
  // currently set to, and whether this machine can take the Parakeet
  // upgrade. `spitball local models --json` -- the picker list (name,
  // engine, installed, size_mb, languages, recommended, active).
  property var localInfo: ({ installed: false, engine: "", model: "", message: "", can_upgrade_parakeet: false })
  property bool localInfoFailed: false
  property var localModels: []
  property bool localModelsFailed: false
  property var summaryModels: []

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
  // popup, or from a previous session, shows up the moment the panel is
  // reopened -- see Model.js's modelSwitch* helpers and Widget.qml's own
  // watch of the same file for the bar-widget side of this.
  readonly property string modelStateFilePath: root.runtimeDir + "/model.json"
  property var modelSwitch: null
  readonly property bool modelSwitchBusy: Model.modelSwitchActive(root.modelSwitch)

  FileView {
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

  function load() {
    root.loading = true
    root.loadFailed = false
    root.runCliJson(["config", "get", "--json"], function(ok, data) {
      root.loading = false
      if (!ok || !data || typeof data !== "object") { root.loadFailed = true; return }
      root.settings = data
    })
    root.loadLocalInfo()
    root.loadLocalModels()
    root.runCliJson(["check", "summary", "--json"], function(ok, data) {
      root.summaryModels = (ok && data && Array.isArray(data.models)) ? data.models : []
    })
  }

  // ------------------------------------------------------------ saving

  // Transient "couldn't save that one" flag, keyed by settings key so only
  // the row that actually failed shows it.
  property string errorKey: ""
  property string errorText: ""

  Timer {
    id: errorClearTimer
    interval: 4000
    onTriggered: { root.errorKey = ""; root.errorText = "" }
  }

  function flashError(key, message) {
    root.errorKey = key
    root.errorText = message || "Couldn't save"
    errorClearTimer.restart()
  }

  // Optimistic: the control already shows the new value the instant it's
  // set; a failed CLI round-trip surfaces as flashError rather than
  // snapping the control back (which reads as the click "not registering").
  function setKey(key, rawValue) {
    var next = Object.assign({}, root.settings)
    next[key] = rawValue
    root.settings = next
    var argValue = typeof rawValue === "boolean" ? (rawValue ? "true" : "false") : String(rawValue)
    root.runCli(["config", "set", key, argValue], function(code, stdoutText, stderrText) {
      if (code !== 0) root.flashError(key, stderrText || "Couldn't save")
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

  function setSecret(key, plaintext) {
    var proc = root._secretRunner.createObject(root, { key: key, secret: plaintext })
    if (!proc) { root.flashError(key, "Couldn't save"); return }
    proc.command = ["/usr/bin/python3", "-I", root.cliPath, "config", "set-secret", key]
    proc.running = true
  }

  // ------------------------------------------------------------ folder picker

  property bool callsDirEditable: false
  property bool notesDirEditable: false

  // Closes the popup before the portal folder-chooser dialog opens (layering
  // rule -- see requestClose()'s doc comment above), then reopens as soon as
  // the picker resolves (picked, canceled, or "no picker available" all
  // count -- the dialog itself is unambiguously gone by the time this
  // callback fires).
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

  // ------------------------------------------------------------ local (voxtype)

  // Voxtype's own installer, for the "not installed" case only -- the model
  // picker below covers everything once it IS installed. Fire-and-forget
  // (there's no completion signal for a detached launch), so this closes the
  // popup and leaves it closed -- see requestClose()'s doc comment: the
  // installer's own window/terminal must not fight our layer-shell popup for
  // stacking, and unlike pickFolder() there's no clear moment to reopen at,
  // so the user reopens Settings by hand once they're done. The poll below
  // still runs so `local info` is fresh the moment they do.
  function installVoxtype() {
    root.requestClose()
    root.runRaw(["/bin/sh", "-c", "command -v omarchy-voxtype-install"], function(code) {
      if (code === 0) Quickshell.execDetached(["omarchy-voxtype-install"])
      else Quickshell.execDetached(["omarchy-launch-floating-terminal-with-presentation", "voxtype setup"])
      root.beginLocalPoll()
    })
  }

  // The model dropdown's inline confirmation row ([Switch] [Cancel], per
  // docs/SPEC-settings-and-providers.md section 2/CONTRACT.md) lands here.
  // `spitball local set-model <name>` starts the switch in the background
  // and returns immediately (spitball/providers/local.py:set_model()) -- but
  // it may still need to show a graphical pkexec prompt, or fall back to a
  // floating terminal, so this closes the popup first regardless (layering
  // rule -- see requestClose()'s doc comment). It does NOT reopen: the
  // switch can run for minutes, and its progress is meant to be watched on
  // the bar widget (Widget.qml's showModelSwitch) until the user reopens
  // Settings by hand, where modelSwitch (above) will already reflect it.
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
    localPollTimer.restart()
  }

  Timer {
    id: localPollTimer
    interval: 3000
    repeat: true
    onTriggered: {
      root.localPollTicks += 1
      root.loadLocalInfo()
      root.loadLocalModels()
      if (root.localPollTicks >= root.localPollMaxTicks) localPollTimer.stop()
    }
  }

  // ================================================================ UI

  // ---- pinned header: stays put while the form below scrolls ----
  Item {
    id: settingsHeader
    anchors.top: parent.top
    anchors.left: parent.left
    anchors.right: parent.right
    height: Math.max(settingsTitle.implicitHeight, settingsCloseButton.implicitHeight)

    Text {
      id: settingsTitle
      textFormat: Text.PlainText
      anchors.left: parent.left
      anchors.verticalCenter: parent.verticalCenter
      text: "Spitball settings"
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.body
      font.bold: true
    }

    Button {
      id: settingsCloseButton
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      text: "✕"
      tooltipText: "Close"
      foreground: root.foreground
      fontFamily: root.fontFamily
      fontSize: Style.font.caption
      onClicked: root.requestClose()
    }
  }

  Flickable {
    id: flick
    anchors.top: settingsHeader.bottom
    anchors.topMargin: Style.space(10)
    anchors.left: parent.left
    anchors.right: parent.right
    anchors.bottom: parent.bottom
    contentWidth: width
    contentHeight: content.implicitHeight
    clip: true
    boundsBehavior: Flickable.StopAtBounds
    interactive: contentHeight > height

    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

    Column {
      id: content
      width: flick.width
      spacing: Style.space(14)

      PanelSectionHeader { text: "RECORDING"; foreground: root.foreground; fontFamily: root.fontFamily }

      Text {
        textFormat: Text.PlainText
        visible: root.loadFailed
        width: parent.width
        text: "Couldn't load settings. Spitball's config commands may not be available yet."
        color: Color.urgent
        wrapMode: Text.WordWrap
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
      }

      // Leads the section (per docs/SPEC-live-transcript.md) -- this is the
      // setting most likely to surprise someone (and the one with the real
      // consent implication), so it comes before anything else here.
      ToggleRow {
        label: "Record automatically when a call starts"
        checked: !!root.value("auto_record", false)
        disabledLook: root.loadFailed
        onToggled: root.setKey("auto_record", !checked)
      }

      Text {
        textFormat: Text.PlainText
        width: parent.width
        text: "Every detected call is recorded without asking. Tell people you're recording."
        color: Qt.darker(root.foreground, 1.4)
        wrapMode: Text.WordWrap
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
      }

      ErrorNote { key: "auto_record" }

      FolderRow {
        label: "Save calls to"
        path: root.value("calls_dir", "")
        editable: root.callsDirEditable
        disabledLook: root.loadFailed
        onChangeRequested: root.pickFolder("calls")
        onOpenRequested: root.runCli(["open-folder"], function() {})
        onPathEdited: function(text) { root.setKey("calls_dir", text) }
      }

      FieldRow {
        label: "Your name"
        text: root.value("my_name", "")
        disabledLook: root.loadFailed
        onCommitted: function(t) { root.setKey("my_name", t) }
      }

      ErrorNote { key: "calls_dir" }
      ErrorNote { key: "my_name" }

      PanelSeparator { foreground: root.foreground }
      PanelSectionHeader { id: transcriptionHeader; text: "TRANSCRIPTION"; foreground: root.foreground; fontFamily: root.fontFamily }

      // Phase 1 (docs/SPEC-settings-and-providers.md section 8): exactly
      // two choices, Local (voxtype) / Deepgram, Local first as the
      // default. OpenAI-compatible/AssemblyAI/Soniox are phase 2 -- see
      // docs/ROADMAP.md and the preserved work under docs/phase2/.
      ButtonGroup {
        width: parent.width
        options: Model.providerOptions()
        value: root.provider
        foreground: root.foreground
        background: Color.popups.background
        fontFamily: root.fontFamily
        onChanged: function(v) { root.setKey("transcription_provider", v) }
      }

      // Applies to every provider, so it lives above the per-provider
      // sections rather than inside any one of them.
      Column {
        width: parent.width
        spacing: Style.space(2)

        Text {
          textFormat: Text.PlainText
          text: "Language"
          color: Qt.darker(root.foreground, 1.4)
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          font.bold: true
        }

        ButtonGroup {
          width: parent.width
          options: Model.languageOptions()
          value: root.languageChoice
          foreground: root.foreground
          background: Color.popups.background
          fontFamily: root.fontFamily
          fontSize: Style.font.bodySmall
          onChanged: function(v) {
            if (v === "en" || v === "auto") root.setKey("language", v)
            // "other": just reveals the code field below; nothing is saved
            // until the user actually types a code and commits it.
          }
        }

        FieldRow {
          visible: root.languageChoice === "other"
          label: "ISO code (e.g. es, fr, de)"
          text: root.languageChoice === "other" ? String(root.value("language", "")) : ""
          disabledLook: root.loadFailed
          onCommitted: function(t) { root.setKey("language", t.trim() || "auto") }
        }

        ErrorNote { key: "language" }
      }

      // ---- Local (voxtype) ----
      // No model list, no downloads: local transcription just uses whatever
      // voxtype is already configured with (`spitball local info --json`).
      Column {
        visible: root.provider === "local"
        width: parent.width
        spacing: Style.space(6)
        topPadding: Style.space(4)

        Text {
          textFormat: Text.PlainText
          visible: root.localInfoFailed
          width: parent.width
          text: "Couldn't check voxtype."
          color: Color.urgent
          wrapMode: Text.WordWrap
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
        }

        Text {
          textFormat: Text.PlainText
          visible: !root.localInfoFailed && root.localInfo.installed
          width: parent.width
          text: Model.localInfoLine(root.localInfo)
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
        }

        // ---- model picker ----
        // One dropdown, no "Upgrade to Parakeet"/"Change model…" buttons.
        // Picking a different model doesn't switch anything by itself -- it
        // reveals the inline confirmation row below (per
        // docs/SPEC-settings-and-providers.md section 2), which is the only
        // thing that actually calls `local set-model`.
        Column {
          visible: !root.localInfoFailed && root.localInfo.installed
          width: parent.width
          spacing: Style.space(4)
          topPadding: Style.space(2)

          Text {
            textFormat: Text.PlainText
            visible: root.localModelsFailed
            width: parent.width
            text: "Couldn't list voxtype models."
            color: Color.urgent
            wrapMode: Text.WordWrap
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          Dropdown {
            visible: !root.localModelsFailed && root.localModels.length > 0
            width: parent.width
            label: "Model"
            options: Model.localModelOptions(root.localModels)
            value: root.pendingModelName
            foreground: root.foreground
            fontFamily: root.fontFamily
            opacity: root.modelSwitchBusy ? 0.5 : 1.0
            // Disabled (in effect) while a switch is already running -- there's
            // nothing sane to confirm on top of one already in flight.
            onChanged: function(v) {
              if (root.modelSwitchBusy) return
              root.pendingModelName = v; root.pendingModelDirty = true
            }
          }

          // ---- inline confirmation ----
          Column {
            visible: !root.localModelsFailed && root.localModels.length > 0 && !root.modelSwitchBusy
              && root.pendingModelName !== Model.activeModelName(root.localModels)
            width: parent.width
            spacing: Style.space(6)
            topPadding: Style.space(2)

            Text {
              textFormat: Text.PlainText
              width: parent.width
              text: Model.switchConfirmText(root.localModels, root.pendingModelName)
              color: root.foreground
              wrapMode: Text.WordWrap
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
            }

            Row {
              spacing: Style.space(8)

              Button {
                text: "Switch"
                bordered: true
                selected: true
                foreground: root.foreground
                fontFamily: root.fontFamily
                fontSize: Style.font.caption
                onClicked: root.confirmSwitchModel()
              }

              Button {
                text: "Cancel"
                bordered: true
                foreground: root.foreground
                fontFamily: root.fontFamily
                fontSize: Style.font.caption
                onClicked: root.cancelSwitchModel()
              }
            }
          }

          // ---- in-flight / just-failed switch status ----
          // Shown whenever model.json says something is (or just went)
          // wrong or in progress -- the same file Widget.qml's bar dot
          // watches, so this and the bar agree. No progress bar for a
          // "switching-engine"/"activating"/"restarting" step (no byte
          // count to show); a "downloading" one gets a real bar.
          Column {
            visible: root.modelSwitch !== null
              && (Model.modelSwitchActive(root.modelSwitch) || Model.modelSwitchFailed(root.modelSwitch))
            width: parent.width
            spacing: Style.space(4)
            topPadding: Style.space(4)

            Text {
              textFormat: Text.PlainText
              width: parent.width
              text: Model.modelSwitchStatusText(root.modelSwitch)
              color: Model.modelSwitchFailed(root.modelSwitch) ? Color.urgent : root.foreground
              wrapMode: Text.WordWrap
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
            }

            Item {
              visible: root.modelSwitch !== null && root.modelSwitch.state === "downloading"
                && Model.modelSwitchPercent(root.modelSwitch) >= 0
              width: parent.width
              height: Style.space(6)

              Rectangle {
                anchors.fill: parent
                radius: height / 2
                color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.15)
              }
              Rectangle {
                anchors.left: parent.left
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                radius: height / 2
                color: root.foreground
                width: parent.width * Math.max(0, Model.modelSwitchPercent(root.modelSwitch)) / 100
              }
            }
          }
        }

        Text {
          textFormat: Text.PlainText
          visible: !root.localInfoFailed && !root.localInfo.installed
          width: parent.width
          text: "Install Omarchy's dictation (voxtype) to transcribe on this computer."
          color: Qt.darker(root.foreground, 1.4)
          wrapMode: Text.WordWrap
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
        }

        Button {
          visible: !root.localInfoFailed && !root.localInfo.installed
          text: "Install"
          bordered: true
          foreground: root.foreground
          fontFamily: root.fontFamily
          fontSize: Style.font.caption
          onClicked: root.installVoxtype()
        }

        Text {
          textFormat: Text.PlainText
          width: parent.width
          text: "Runs on this computer. Slower than cloud; audio never leaves your machine."
          color: Qt.darker(root.foreground, 1.4)
          wrapMode: Text.WordWrap
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          topPadding: Style.space(4)
        }
      }

      // ---- Deepgram ----
      Column {
        visible: root.provider === "deepgram"
        width: parent.width
        spacing: Style.space(8)
        topPadding: Style.space(4)

        SecretRow {
          label: "API key"
          placeholder: Model.keySourcePlaceholder(root.secretInfo("deepgram_api_key"))
          disabledLook: root.loadFailed
          onSaved: function(t) { root.setSecret("deepgram_api_key", t) }
        }

        FieldRow {
          label: "Model"
          text: root.value("deepgram_model", "nova-3")
          disabledLook: root.loadFailed
          onCommitted: function(t) { root.setKey("deepgram_model", t) }
        }

        TestRow {
          busy: root.deepgramTesting
          message: root.deepgramTestMsg
          disabledLook: root.loadFailed
          onRun: root.testTranscription("deepgram")
        }

        ErrorNote { key: "deepgram_api_key" }
        ErrorNote { key: "deepgram_model" }
      }

      PanelSeparator { foreground: root.foreground }
      PanelSectionHeader { text: "SUMMARIES"; foreground: root.foreground; fontFamily: root.fontFamily }

      ToggleRow {
        label: "Summarize calls"
        checked: !!root.value("summary_enabled", true)
        disabledLook: root.loadFailed
        onToggled: root.setKey("summary_enabled", !checked)
      }

      Column {
        visible: !!root.value("summary_enabled", true)
        width: parent.width
        spacing: Style.space(8)

        FieldRow {
          label: "Endpoint URL"
          text: root.value("summary_base_url", "")
          disabledLook: root.loadFailed
          onCommitted: function(t) { root.setKey("summary_base_url", t) }
        }

        Dropdown {
          visible: root.summaryModels.length > 0
          width: parent.width
          label: "Model"
          options: root.summaryModels.map(function(m) { return typeof m === "object" ? m : { value: String(m), label: String(m) } })
          value: String(root.value("summary_model", ""))
          foreground: root.foreground
          fontFamily: root.fontFamily
          onChanged: function(v) { root.setKey("summary_model", v) }
        }

        FieldRow {
          visible: root.summaryModels.length === 0
          label: "Model"
          text: root.value("summary_model", "")
          disabledLook: root.loadFailed
          onCommitted: function(t) { root.setKey("summary_model", t) }
        }

        SecretRow {
          label: "API key"
          placeholder: Model.keySourcePlaceholder(root.secretInfo("summary_api_key"))
          disabledLook: root.loadFailed
          onSaved: function(t) { root.setSecret("summary_api_key", t) }
        }

        TestRow {
          busy: root.summaryTesting
          message: root.summaryTestMsg
          disabledLook: root.loadFailed
          onRun: root.testSummary()
        }

        ErrorNote { key: "summary_base_url" }
        ErrorNote { key: "summary_model" }
        ErrorNote { key: "summary_api_key" }
      }
      ErrorNote { key: "summary_enabled" }

      PanelSeparator { foreground: root.foreground }
      PanelSectionHeader { text: "NOTES COPY"; foreground: root.foreground; fontFamily: root.fontFamily }

      ToggleRow {
        label: "Also copy notes to a folder"
        checked: String(root.value("export_dir", "")) !== ""
        disabledLook: root.loadFailed
        onToggled: {
          if (checked) root.setKey("export_dir", "")
          else root.pickFolder("export")
        }
      }

      FolderRow {
        visible: String(root.value("export_dir", "")) !== ""
        label: "Notes folder"
        path: root.value("export_dir", "")
        editable: root.notesDirEditable
        disabledLook: root.loadFailed
        showOpen: false
        onChangeRequested: root.pickFolder("export")
        onPathEdited: function(text) { root.setKey("export_dir", text) }
      }

      Text {
        textFormat: Text.PlainText
        width: parent.width
        text: "Audio is never copied."
        color: Qt.darker(root.foreground, 1.4)
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
      }

      ErrorNote { key: "export_dir" }
    }
  }

  // ================================================================ row components

  component ErrorNote: Text {
    property string key: ""
    textFormat: Text.PlainText
    visible: root.errorKey === key && root.errorText !== ""
    width: parent.width
    text: root.errorText
    color: Color.urgent
    wrapMode: Text.WordWrap
    font.family: root.fontFamily
    font.pixelSize: Style.font.caption
  }

  component FieldRow: Column {
    id: fieldRow
    property string label: ""
    property string text: ""
    property bool disabledLook: false
    signal committed(string text)

    width: parent.width
    spacing: Style.space(2)

    Text {
      textFormat: Text.PlainText
      text: fieldRow.label
      color: Qt.darker(root.foreground, 1.4)
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      font.bold: true
    }

    TextField {
      id: field
      width: parent.width
      text: fieldRow.text
      enabled: !fieldRow.disabledLook
      foreground: root.foreground
      font.pixelSize: Style.font.body
      onEditingFinished: fieldRow.committed(text)

      // Keep the field in sync when the underlying value changes from
      // elsewhere (a fresh load(), an optimistic update from another
      // control) without stomping on what the user is mid-typing.
      Connections {
        target: fieldRow
        function onTextChanged() { if (!field.activeFocus) field.text = fieldRow.text }
      }
    }
  }

  component SecretRow: Column {
    id: secretRow
    property string label: ""
    property string placeholder: "Not set"
    property bool disabledLook: false
    signal saved(string text)

    width: parent.width
    spacing: Style.space(2)

    Text {
      textFormat: Text.PlainText
      text: secretRow.label
      color: Qt.darker(root.foreground, 1.4)
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      font.bold: true
    }

    Row {
      width: parent.width
      spacing: Style.space(6)

      TextField {
        id: secretField
        width: parent.width - saveBtn.width - parent.spacing
        password: true
        enabled: !secretRow.disabledLook
        placeholderText: secretRow.placeholder
        foreground: root.foreground
        font.pixelSize: Style.font.body
        onAccepted: {
          if (text !== "") { secretRow.saved(text); text = "" }
        }
      }

      Button {
        id: saveBtn
        text: "Save"
        bordered: true
        enabled: !secretRow.disabledLook && secretField.text !== ""
        foreground: root.foreground
        fontFamily: root.fontFamily
        fontSize: Style.font.caption
        onClicked: { secretRow.saved(secretField.text); secretField.text = "" }
      }
    }
  }

  component ToggleRow: Item {
    property string label: ""
    property bool checked: false
    property bool disabledLook: false
    signal toggled()

    width: parent.width
    height: Style.space(26)

    Text {
      textFormat: Text.PlainText
      anchors.left: parent.left
      anchors.right: sw.left
      anchors.rightMargin: Style.space(10)
      anchors.verticalCenter: parent.verticalCenter
      text: parent.label
      color: root.foreground
      opacity: parent.disabledLook ? 0.5 : (parent.checked ? 0.9 : 0.65)
      font.family: root.fontFamily
      font.pixelSize: Style.font.body
      elide: Text.ElideRight
    }

    ToggleSwitch {
      id: sw
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      checked: parent.checked
      interactive: !parent.disabledLook
      trackHeight: Style.space(18)
      foreground: root.foreground
      onToggled: parent.toggled()
    }
  }

  component TestRow: Item {
    property bool busy: false
    property string message: ""
    property bool disabledLook: false
    signal run()

    width: parent.width
    height: Math.max(testBtn.implicitHeight, resultLabel.implicitHeight)

    Button {
      id: testBtn
      text: parent.busy ? "Testing…" : "Test"
      bordered: true
      enabled: !parent.disabledLook && !parent.busy
      foreground: root.foreground
      fontFamily: root.fontFamily
      fontSize: Style.font.caption
      onClicked: parent.run()
    }

    Text {
      id: resultLabel
      textFormat: Text.PlainText
      visible: parent.message !== ""
      anchors.left: testBtn.right
      anchors.leftMargin: Style.space(8)
      anchors.right: parent.right
      anchors.verticalCenter: testBtn.verticalCenter
      text: parent.message
      color: parent.message.indexOf("✓") === 0 ? root.foreground : Color.urgent
      wrapMode: Text.WordWrap
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }
  }

  component FolderRow: Column {
    id: folderRow
    property string label: ""
    property string path: ""
    property bool editable: false
    property bool disabledLook: false
    property bool showOpen: true
    signal changeRequested()
    signal openRequested()
    signal pathEdited(string text)

    width: parent.width
    spacing: Style.space(2)

    Text {
      textFormat: Text.PlainText
      text: folderRow.label
      color: Qt.darker(root.foreground, 1.4)
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      font.bold: true
    }

    Row {
      width: parent.width
      spacing: Style.space(6)

      TextField {
        id: pathField
        visible: folderRow.editable
        width: parent.width - changeBtn.width - (openBtn.visible ? openBtn.width + parent.spacing : 0) - parent.spacing * (openBtn.visible ? 2 : 1)
        text: folderRow.path
        enabled: !folderRow.disabledLook
        foreground: root.foreground
        font.pixelSize: Style.font.bodySmall
        onEditingFinished: folderRow.pathEdited(text)
      }

      Text {
        textFormat: Text.PlainText
        visible: !folderRow.editable
        width: parent.width - changeBtn.width - (openBtn.visible ? openBtn.width + parent.spacing : 0) - parent.spacing * (openBtn.visible ? 2 : 1)
        anchors.verticalCenter: changeBtn.verticalCenter
        text: folderRow.path !== "" ? folderRow.path : "(default)"
        color: root.foreground
        opacity: 0.8
        elide: Text.ElideMiddle
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
      }

      Button {
        id: changeBtn
        text: "Change…"
        bordered: true
        enabled: !folderRow.disabledLook
        foreground: root.foreground
        fontFamily: root.fontFamily
        fontSize: Style.font.caption
        onClicked: folderRow.changeRequested()
      }

      Button {
        id: openBtn
        visible: folderRow.showOpen
        text: "Open"
        bordered: true
        enabled: !folderRow.disabledLook
        foreground: root.foreground
        fontFamily: root.fontFamily
        fontSize: Style.font.caption
        onClicked: folderRow.openRequested()
      }
    }
  }
}
