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

  // "detected"/"recording"/"processing"/"error" are always visible; only
  // idle/offline collapse and wait for a hover reveal.
  readonly property bool shouldShow: !isIdleLike || inactiveRevealed

  property double nowMs: Date.now()
  readonly property string elapsedText: Model.elapsedLabel(st.started_at, nowMs)

  readonly property string glyphChar: isProcessing ? "◐" : (isError ? "×" : "●")
  readonly property color glyphColor: isRecording ? root.urgent
    : isDetected ? root.amber
    : isError ? root.urgent
    : isProcessing ? root.dim
    : root.dim
  readonly property string labelText: isDetected ? (st.app || "")
    : isRecording ? root.elapsedText
    : ""

  function applyState(text) {
    root.st = Model.parseState(text)
  }

  // The plugin's own CLI, by absolute path, so nothing needs to be on PATH.
  readonly property string cliPath: Qt.resolvedUrl("bin/spitball").toString().replace(/^file:\/\//, "")

  // Fire-and-forget CLI call. If the daemon isn't up yet the CLI just prints an
  // error nobody sees; the widget keeps showing whatever state.json says.
  function runCli(args) {
    Quickshell.execDetached(["/usr/bin/python3", "-I", root.cliPath].concat(args))
  }

  function handleLeftClick() {
    if (root.isRecording) root.runCli(["stop"])
    else if (root.isError) root.runCli(["open-folder"])
    else if (root.isIdleLike || root.isDetected) root.runCli(["start"])
    // processing: no click action.
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
      tooltipText: Model.tooltipFor(root.st)
      concealed: !root.shouldShow
      interactive: root.shouldShow
      keepSpace: false
      dimmed: root.isIdleLike
      fixedWidth: vertical ? -1 : Math.round(barContent.implicitWidth + Style.spaceReal(7) * 2)
      fixedHeight: vertical ? Math.round(barContent.implicitHeight + Style.spaceReal(6) * 2) : -1

      onPressed: function(b) {
        if (b === Qt.RightButton) root.toggle()
        else if (b === Qt.LeftButton) root.handleLeftClick()
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
  // The widget's whole UI on right click: Start/Stop, Dismiss (only while
  // detected), Auto-record toggle, Open last summary, Open calls folder.
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
          leftAlign: true
          iconText: "●"
          text: root.isRecording ? "Stop recording" : "Start recording"
          foreground: root.panelForeground
          fontFamily: root.fontFamily
          onClicked: { root.runCli([root.isRecording ? "stop" : "start"]); root.close() }
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
      }
    }
  }
}
