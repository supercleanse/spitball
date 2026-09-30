import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

// Spitball bar widget: a thin view over the spitball daemon. It reads
// $XDG_RUNTIME_DIR/spitball/state.json (watched via FileView, never polled)
// and renders one of six states -- offline/idle/detected/recording/
// processing/error -- as a glyph plus an optional label, per
// programs/Spitball/CONTRACT.md. Every action shells out to the
// `spitball` CLI; this file has no recording/transcription logic of its own,
// and never assumes the CLI exists (a missing binary just fails to spawn).
Panel {
  id: root
  moduleName: "supercleanse.spitball"

  // ------------------------------------------------------------ geometry
  // Panel doesn't define these (unlike BarWidget), but the bar item below
  // needs them outside the button's own scope, so lift them here the same
  // way BarWidget.qml does for every other widget.
  readonly property bool vertical: bar ? bar.vertical : false
  readonly property int barSize: bar ? bar.barSize : Style.bar.sizeHorizontal

  // ------------------------------------------------------------ paths
  readonly property string runtimeDir: Quickshell.env("XDG_RUNTIME_DIR") || "/run/user/1000"
  readonly property string stateFilePath: runtimeDir + "/spitball/state.json"
  readonly property string modelStateFilePath: runtimeDir + "/spitball/model.json"

  // ------------------------------------------------------------ theme
  readonly property color foreground: bar ? bar.barForeground : Color.foreground
  readonly property color panelForeground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.5)
  readonly property color amber: "#d9a441"
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  // ------------------------------------------------------------ reveal
  // Mirrors omarchy.indicators' inactive-indicator collapse exactly: hidden
  // (zero width) unless the center section is hover-revealed, or this
  // widget's own area is hovered directly (belt-and-suspenders -- the bar's
  // center-wide hover already covers us since we live in bar.layout.center,
  // but a local hover keeps this correct even if the widget is ever moved).
  property bool selfHovered: false
  readonly property bool centerRevealed: !!bar && bar.centerSectionRevealHeld === true && bar.centerHoverRevealSuppressed !== true
  readonly property bool inactiveRevealed: selfHovered || centerRevealed

  // ------------------------------------------------------------ state
  // Named callState, not state -- every QML Item already has a built-in
  // `state` property (the Qt States/Transitions system), and redeclaring it
  // would be a compile error.
  property var st: Model.emptyState()
  readonly property string callState: st.state
  readonly property bool isIdleLike: callState === "idle" || callState === "offline"
  readonly property bool isDetected: callState === "detected"
  readonly property bool isRecording: callState === "recording"
  readonly property bool isProcessing: callState === "processing"
  readonly property bool isError: callState === "error"

  // Idle with a daemon-reported setup_needed reason (missing key, no local
  // model downloaded, voxtype not installed...) -- spec section 4: shown as
  // a small gear glyph even though plain idle would otherwise collapse.
  readonly property bool needsSetup: Model.needsSetup(st)

  // A background `spitball local set-model` switch (see
  // spitball/providers/local.py's set_model()/run_set_model_worker() and
  // CONTRACT.md) reports its own progress to model.json, independent of the
  // daemon's state.json/CONTRACT states above. This is intentionally NEVER
  // allowed to override a real call state (recording/detected/processing/
  // error(call)) -- showModelSwitch is gated on isIdleLike, the same slot
  // needsSetup already occupies, so it only ever shows when there's
  // genuinely nothing more important going on.
  property var modelSwitch: null
  readonly property bool showModelSwitch: root.isIdleLike
    && (Model.modelSwitchActive(root.modelSwitch) || Model.modelSwitchFailed(root.modelSwitch))

  // "detected"/"recording"/"processing"/"error" are always visible; only
  // idle/offline collapse and wait for a hover reveal. needsSetup (and a
  // live/failed model switch) force the idle gear to stay visible too, so
  // neither a fresh install nor an in-flight model switch is ever invisible.
  readonly property bool shouldShow: !isIdleLike || inactiveRevealed || needsSetup || showModelSwitch

  property double nowMs: Date.now()
  readonly property string elapsedText: Model.elapsedLabel(st.started_at, nowMs)

  readonly property string glyphChar: root.showModelSwitch
    ? (Model.modelSwitchActive(root.modelSwitch) ? "" : "×")
    : root.needsSetup ? ""
    : isProcessing ? "◐" : (isError ? "×" : "●")
  readonly property color glyphColor: isRecording ? root.urgent
    : isDetected ? root.amber
    : isError ? root.urgent
    : root.showModelSwitch ? root.amber
    : root.needsSetup ? root.amber
    : isProcessing ? root.dim
    : root.dim
  readonly property string labelText: isDetected ? (st.app || "")
    : isRecording ? root.elapsedText
    : ""

  function applyState(text) {
    root.st = Model.parseState(text)
  }

  function applyModelState(text) {
    root.modelSwitch = Model.parseModelState(text)
  }

  // The plugin's own CLI, by absolute path, so nothing needs to be on PATH.
  readonly property string cliPath: Qt.resolvedUrl("bin/spitball").toString().replace(/^file:\/\//, "")

  // Fire-and-forget CLI call. If the daemon isn't up yet the CLI just prints an
  // error nobody sees; the widget keeps showing whatever state.json says.
  function runCli(args) {
    Quickshell.execDetached(["/usr/bin/python3", "-I", root.cliPath].concat(args))
  }

  // A click on the bar -- left or right -- only ever opens the menu. Every
  // action (start, stop, the live transcript, settings) is an item in it,
  // so there's one gesture to learn. Per docs/SPEC-live-transcript.md a bar
  // click must never stop a recording (a user once clicked the dot
  // expecting a menu and killed their own call); Stop is only the menu item
  // or the Live popup's own button.
  //
  // Starting a recording from the menu opens the Live popup as well, so
  // the transcript is already on screen the moment recording begins.
  function startRecording() {
    root.runCli(["start"])
    root.openLive()
  }

  // The first-run gear, or a failed model switch: the menu leads with a
  // way into Settings' Transcription section. A switch still in progress
  // has nothing to do but wait.
  readonly property bool menuShowsSetup: root.needsSetup
    || (root.showModelSwitch && !Model.modelSwitchActive(root.modelSwitch))

  // ------------------------------------------------------------ settings panel
  // A second, independent popup from the menu's -- both use
  // KeyboardPanel, but this one is opened by its own bool rather than the
  // base Panel's `opened`/`controller` (which the menu already owns).
  property bool settingsOpened: false

  function openSettings() {
    root.close()  // the menu, if it's what triggered this
    root.settingsOpened = true
  }

  // Per docs/SPEC-settings-and-providers.md section 6: the first-run gear
  // opens Settings scrolled straight to Transcription, since that's where
  // the setup_needed reason (missing key / no local model / voxtype
  // missing) always gets resolved.
  function openSettingsToTranscription() {
    root.openSettings()
    if (settingsPanel && settingsPanel.settingsContent) settingsPanel.settingsContent.scrollToTranscription()
  }

  // ------------------------------------------------------------ live popup
  // A third, independent popup (same KeyboardPanel component as the menu and
  // settings above) -- driven by its own `liveOpened` bool so it can be open
  // at the same time as neither/either of the others without fighting over
  // state. See LivePopup.qml and docs/SPEC-live-transcript.md.
  property bool liveOpened: false

  function openLive() {
    root.close()  // the menu, if it's what triggered this
    root.liveOpened = true
  }

  // Opens the Live popup from outside the shell, e.g. for screenshots:
  //   omarchy-shell supercleanse.spitball-settings live
  IpcHandler {
    target: "supercleanse.spitball-live"
    function open(): string { root.openLive(); return "ok" }
    function close(): string { root.liveOpened = false; return "ok" }
    function toggle(): string { root.liveOpened = !root.liveOpened; return "ok" }
  }

  // Opens the settings panel from outside the shell, e.g. for screenshots:
  //   omarchy-shell supercleanse.spitball-settings open
  // A distinct target from the service's "supercleanse.spitball" (which
  // already owns `restart`) -- Widget.qml and SpitballService.qml are
  // separate entry points with no shared scope to dispatch through one
  // handler, and Quickshell IpcHandler targets are one-to-one with a single
  // handler in this codebase (every built-in plugin claims its target
  // exactly once).
  IpcHandler {
    target: "supercleanse.spitball-settings"
    function open(): string { root.openSettings(); return "ok" }
    function close(): string { root.settingsOpened = false; return "ok" }
    function toggle(): string { root.settingsOpened = !root.settingsOpened; return "ok" }
  }

  // ------------------------------------------------------------ plumbing
  FileView {
    id: stateFile
    path: root.stateFilePath
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: root.applyState(text())
    onLoadFailed: function(error) { root.applyState("") }
  }

  // A background model switch (spitball local set-model) writes here; see
  // showModelSwitch above. Missing/unparsable just means "no switch running"
  // -- never an error state of its own.
  FileView {
    id: modelStateFile
    path: root.modelStateFilePath
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: root.applyModelState(text())
    onLoadFailed: function(error) { root.applyModelState("") }
  }

  // Elapsed time only needs to tick while a call is actually recording.
  Timer {
    interval: 1000
    running: root.isRecording
    repeat: true
    onTriggered: root.nowMs = Date.now()
  }
  onIsRecordingChanged: if (isRecording) root.nowMs = Date.now()

  visible: true
  clip: true
  implicitWidth: collapseArea.implicitWidth
  implicitHeight: collapseArea.implicitHeight

  // ------------------------------------------------------------ bar item
  // Two layers, same shape as omarchy.indicators' IndicatorBlock: an outer
  // Item whose implicit size collapses to zero (clipped) when hidden, and
  // an inner button that always keeps its natural size so the reveal is a
  // clean width animation rather than a re-layout.
  Item {
    id: collapseArea
    clip: true
    implicitWidth: root.vertical ? Math.max(button.implicitWidth, root.barSize)
      : (root.shouldShow ? button.implicitWidth : 0)
    implicitHeight: root.vertical ? (root.shouldShow ? button.implicitHeight : 0)
      : Math.max(button.implicitHeight, root.barSize)
    width: implicitWidth
    height: implicitHeight

    HoverHandler {
      onHoveredChanged: root.selfHovered = hovered
    }

    WidgetButton {
      id: button
      anchors.centerIn: parent
      bar: root.bar
      text: " "
      labelVisible: false
      hasVisualContent: true
      tooltipText: root.showModelSwitch ? Model.modelSwitchStatusText(root.modelSwitch) : Model.tooltipFor(root.st)
      concealed: !root.shouldShow
      interactive: root.shouldShow
      keepSpace: false
      dimmed: root.isIdleLike
      fixedWidth: vertical ? -1 : Math.round(barContent.implicitWidth + Style.spaceReal(7) * 2)
      fixedHeight: vertical ? Math.round(barContent.implicitHeight + Style.spaceReal(6) * 2) : -1

      onPressed: function(b) {
        if (b === Qt.LeftButton || b === Qt.RightButton) root.toggle()
      }

      Grid {
        id: barContent
        anchors.centerIn: parent
        columns: button.vertical ? 1 : 2
        rowSpacing: Style.space(2)
        columnSpacing: Style.space(5)
        horizontalItemAlignment: Grid.AlignHCenter
        verticalItemAlignment: Grid.AlignVCenter

        Text {
          id: glyphText
          textFormat: Text.PlainText
          text: root.glyphChar
          color: root.glyphColor
          font.family: root.fontFamily
          font.pixelSize: Style.bar.iconFont
          renderType: Text.NativeRendering

          // "Gently pulsing" per the contract -- only while a detection is
          // pending a decision; every other state holds a steady glyph.
          SequentialAnimation on opacity {
            loops: Animation.Infinite
            running: root.isDetected
            NumberAnimation { from: 1.0; to: 0.4; duration: 650; easing.type: Easing.InOutSine }
            NumberAnimation { from: 0.4; to: 1.0; duration: 650; easing.type: Easing.InOutSine }
          }
        }

        Text {
          textFormat: Text.PlainText
          visible: root.labelText !== ""
          text: root.labelText
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
          renderType: Text.NativeRendering
        }
      }
    }
  }

  // ------------------------------------------------------------ popup menu
  // The widget's whole UI, on any click: Set up (only when needed),
  // Start / Show live transcript + Stop, Dismiss (only while detected),
  // Auto-record toggle, Open last summary, Open calls folder, Settings.
  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(260))
    contentHeight: panel.fittedContentHeight(menuColumn.implicitHeight, Style.space(360))

    FocusScope {
      id: keyCatcher
      anchors.fill: parent
      focus: true
      Keys.onEscapePressed: root.close()

      Column {
        id: menuColumn
        width: parent.width
        spacing: Style.space(2)

        Button {
          width: parent.width
          visible: root.menuShowsSetup
          leftAlign: true
          iconText: ""
          text: "Set up transcription…"
          foreground: root.panelForeground
          fontFamily: root.fontFamily
          onClicked: root.openSettingsToTranscription()
        }

        Button {
          width: parent.width
          visible: !root.isRecording
          leftAlign: true
          iconText: "●"
          text: "Start recording"
          foreground: root.panelForeground
          fontFamily: root.fontFamily
          onClicked: root.startRecording()
        }

        Button {
          width: parent.width
          visible: root.isRecording
          leftAlign: true
          iconText: "󰍡"
          text: "Show live transcript"
          foreground: root.panelForeground
          fontFamily: root.fontFamily
          onClicked: root.openLive()
        }

        Button {
          width: parent.width
          visible: root.isRecording
          leftAlign: true
          iconText: "■"
          text: "Stop recording"
          foreground: root.panelForeground
          fontFamily: root.fontFamily
          onClicked: { root.runCli(["stop"]); root.close() }
        }

        Button {
          width: parent.width
          visible: root.isDetected
          leftAlign: true
          text: "Dismiss"
          foreground: root.panelForeground
          fontFamily: root.fontFamily
          onClicked: { root.runCli(["dismiss"]); root.close() }
        }

        PanelSeparator { width: parent.width; foreground: root.panelForeground }

        Button {
          width: parent.width
          leftAlign: true
          selected: root.st.auto_record === true
          iconText: root.st.auto_record === true ? "✓" : ""
          text: "Auto-record on detect"
          foreground: root.panelForeground
          fontFamily: root.fontFamily
          onClicked: { root.runCli(["auto", "toggle"]); root.close() }
        }

        PanelSeparator { width: parent.width; foreground: root.panelForeground }

        Button {
          width: parent.width
          leftAlign: true
          enabled: root.st.last_call !== null
          opacity: enabled ? 1.0 : 0.45
          text: Model.lastCallLabel(root.st.last_call)
          foreground: root.panelForeground
          fontFamily: root.fontFamily
          onClicked: { root.runCli(["open-last"]); root.close() }
        }

        Button {
          width: parent.width
          leftAlign: true
          text: "Open calls folder"
          foreground: root.panelForeground
          fontFamily: root.fontFamily
          onClicked: { root.runCli(["open-folder"]); root.close() }
        }

        PanelSeparator { width: parent.width; foreground: root.panelForeground }

        // Reachable from every state, per spec section 4 ("Settings stays
        // reachable from the menu in every state").
        Button {
          width: parent.width
          leftAlign: true
          iconText: ""
          text: "Settings…"
          foreground: root.panelForeground
          fontFamily: root.fontFamily
          onClicked: root.openSettings()
        }
      }
    }
  }

  // ------------------------------------------------------------ settings popup
  // A second, independent KeyboardPanel -- driven by `root.settingsOpened`
  // rather than the menu's `root.opened`/`controller`, so either one can be
  // open without disturbing the other's state. Wider/taller than the menu,
  // since it holds a full settings form (SettingsPanel.qml). This is the
  // same KeyboardPanel component omarchy.audio/omarchy.network/omarchy.power
  // use for their own popups (see e.g.
  // /usr/share/omarchy/shell/plugins/panels/audio/Panel.qml) -- Settings
  // stays a normal bar dropdown popup, not a separate desktop window.
  KeyboardPanel {
    id: settingsPanel
    property alias settingsContent: settingsContentItem
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.settingsOpened
    focusTarget: settingsKeyCatcher
    contentWidth: settingsPanel.fittedContentWidth(Style.space(380))
    // No extra cap beyond fittedContentHeight's own screen-fit (matches the
    // built-in panels' convention -- network/power/etc. pass no cap either):
    // this is a fixed set of sections, not an unbounded list that needs one.
    // The Flickable inside SettingsPanel is still there as a safety net for
    // a screen too short for the whole form at once.
    contentHeight: settingsPanel.fittedContentHeight(settingsContentItem.contentHeight)

    FocusScope {
      id: settingsKeyCatcher
      anchors.fill: parent
      focus: true
      Keys.onEscapePressed: root.settingsOpened = false

      SettingsPanel {
        id: settingsContentItem
        anchors.fill: parent
        foreground: root.panelForeground
        fontFamily: root.fontFamily
        cliPath: root.cliPath
        runtimeDir: root.runtimeDir + "/spitball"
        active: root.settingsOpened
        onRequestClose: root.settingsOpened = false
        onRequestReopen: root.settingsOpened = true
      }
    }
  }

  // ------------------------------------------------------------ live popup
  // A fourth (menu, settings, and this) KeyboardPanel -- the Live popup:
  // chat-style live transcript + a Stop recording button, per
  // docs/SPEC-live-transcript.md. Taller than the menu (it's a scrollable
  // transcript, not a handful of rows) but similar width to Settings.
  KeyboardPanel {
    id: livePanel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.liveOpened
    focusTarget: liveKeyCatcher
    contentWidth: livePanel.fittedContentWidth(Style.space(340))
    contentHeight: livePanel.fittedContentHeight(Style.space(360))

    FocusScope {
      id: liveKeyCatcher
      anchors.fill: parent
      focus: true
      Keys.onEscapePressed: root.liveOpened = false

      LivePopup {
        id: liveContentItem
        anchors.fill: parent
        foreground: root.panelForeground
        fontFamily: root.fontFamily
        accent: root.urgent
        cliPath: root.cliPath
        runtimeDir: root.runtimeDir + "/spitball"
        active: root.liveOpened
        st: root.st
        onRequestClose: root.liveOpened = false
      }
    }
  }
}
