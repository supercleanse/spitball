import QtQuick
import qs.Commons
import qs.Ui
import "../Model.js" as Model

// Live: the mid-call transcript (live_transcript, live_max_window_s), the
// fast engine toggle (live_engine), and whether the engine is actually
// installed, with a button to install it.
SettingsPage {
  id: page
  title: "Live"

  Component.onCompleted: page.store.loadLiveStatus()

  ToggleRow {
    store: page.store
    label: "Show a live transcript while recording"
    checked: !!page.store.value("live_transcript", true)
    disabledLook: page.store.loadFailed
    onToggled: page.store.setKey("live_transcript", !checked)
  }
  NoteText {
    store: page.store
    text: "Always runs on this computer with voxtype, whatever the transcription provider. It never costs money or sends audio anywhere mid-call."
  }
  ErrorNote { store: page.store; key: "live_transcript" }

  NumberRow {
    store: page.store
    visible: !!page.store.value("live_transcript", true)
    label: "Cut an unbroken stretch of talk into a line after"
    value: Number(page.store.value("live_max_window_s", 12))
    from: 3; to: 60; unit: "s"
    disabledLook: page.store.loadFailed
    onCommitted: function(v) { page.store.setKey("live_max_window_s", v) }
  }
  ErrorNote { store: page.store; key: "live_max_window_s" }

  PanelSeparator { foreground: page.store.foreground }

  ToggleRow {
    store: page.store
    label: "Use the fast engine when it's installed"
    checked: !!page.store.value("live_engine", true)
    disabledLook: page.store.loadFailed
    onToggled: page.store.setKey("live_engine", !checked)
  }
  NoteText {
    store: page.store
    text: "Keeps voxtype's Parakeet model loaded for the whole call, so lines land under a second after you speak. Off, or without the engine, each line is one voxtype run a few seconds after each pause."
  }
  ErrorNote { store: page.store; key: "live_engine" }

  Item {
    width: parent.width
    height: Math.max(statusLine.implicitHeight, installButton.implicitHeight)

    Text {
      id: statusLine
      textFormat: Text.PlainText
      anchors.left: parent.left
      anchors.right: installButton.left
      anchors.rightMargin: Style.space(10)
      anchors.verticalCenter: parent.verticalCenter
      text: page.store.liveStatusFailed ? "Couldn't check the fast engine."
        : (page.store.liveStatus === null ? "Checking the fast engine…" : Model.liveEngineStatusLine(page.store.liveStatus))
      color: page.store.liveStatusFailed ? Color.urgent : page.store.foreground
      wrapMode: Text.WordWrap
      font.family: page.store.fontFamily
      font.pixelSize: Style.font.body
    }

    Button {
      id: installButton
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      visible: page.store.liveStatus !== null && !page.store.liveStatus.installed
      text: "Install"
      bordered: true
      foreground: page.store.foreground
      fontFamily: page.store.fontFamily
      fontSize: Style.font.caption
      onClicked: page.store.installLiveEngine()
    }
  }
  NoteText {
    store: page.store
    text: "Installing creates a small virtualenv (about 130 MB) under ~/.local/share/spitball/live-engine/ with onnx-asr and onnxruntime. It needs voxtype on a Parakeet model to take effect."
  }
}
