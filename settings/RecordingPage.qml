pragma ComponentBehavior: Bound
import QtQuick
import qs.Commons
import qs.Ui
import "../Model.js" as Model

// Recording: where calls go, the detection/ending timings, the Opus
// bitrate, which apps count as a call, and a read-out of who's on the mic
// right now for troubleshooting.
SettingsPage {
  id: page
  title: "Recording"

  Component.onCompleted: page.store.loadPresentApps()

  FolderRow {
    store: page.store
    label: "Save calls to"
    path: page.store.value("calls_dir", "")
    editable: page.store.callsDirEditable
    disabledLook: page.store.loadFailed
    onChangeRequested: page.store.pickFolder("calls")
    onOpenRequested: page.store.openCallsFolder()
    onPathEdited: function(text) { page.store.setKey("calls_dir", text) }
  }
  ErrorNote { store: page.store; key: "calls_dir" }

  PanelSeparator { foreground: page.store.foreground }

  // Timings, two per row -- each is a small integer, so the number fields
  // sit side by side rather than stacking into a long column.
  Flow {
    width: parent.width
    spacing: Style.space(20)

    NumberRow {
      store: page.store
      width: (parent.width - parent.spacing) / 2
      label: "Detect a call after"
      value: Number(page.store.value("detect_after_s", 2))
      from: 0; to: 60; unit: "s"
      disabledLook: page.store.loadFailed
      onCommitted: function(v) { page.store.setKey("detect_after_s", v) }
    }

    NumberRow {
      store: page.store
      width: (parent.width - parent.spacing) / 2
      label: "Call over after"
      value: Number(page.store.value("end_after_s", 8))
      from: 1; to: 120; unit: "s"
      disabledLook: page.store.loadFailed
      onCommitted: function(v) { page.store.setKey("end_after_s", v) }
    }

    NumberRow {
      store: page.store
      width: (parent.width - parent.spacing) / 2
      label: "Shortest detected call to keep"
      value: Number(page.store.value("min_call_s", 60))
      from: 0; to: 3600; stepSize: 5; unit: "s"
      disabledLook: page.store.loadFailed
      onCommitted: function(v) { page.store.setKey("min_call_s", v) }
    }

    NumberRow {
      store: page.store
      width: (parent.width - parent.spacing) / 2
      label: "Shortest manual recording to keep"
      value: Number(page.store.value("min_manual_s", 10))
      from: 0; to: 3600; stepSize: 5; unit: "s"
      disabledLook: page.store.loadFailed
      onCommitted: function(v) { page.store.setKey("min_manual_s", v) }
    }
  }
  NoteText {
    store: page.store
    text: "Detection waits for the mic to stay open; ending rides out brief device switches. Short recordings are assumed to be a huddle you clicked out of, not a call."
  }
  ErrorNote { store: page.store; key: "detect_after_s" }
  ErrorNote { store: page.store; key: "end_after_s" }
  ErrorNote { store: page.store; key: "min_call_s" }
  ErrorNote { store: page.store; key: "min_manual_s" }

  Dropdown {
    width: Style.space(200)
    label: "Opus bitrate"
    options: Model.opusBitrateOptions(page.store.value("opus_bitrate", "32k"))
    value: String(page.store.value("opus_bitrate", "32k"))
    foreground: page.store.foreground
    fontFamily: page.store.fontFamily
    onChanged: function(v) { page.store.setKey("opus_bitrate", v) }
  }
  NoteText { store: page.store; text: "32k is plenty for speech. Higher costs disk; lower starts to hurt transcription." }
  ErrorNote { store: page.store; key: "opus_bitrate" }

  PanelSeparator { foreground: page.store.foreground }

  // ---- call apps ----
  Text {
    textFormat: Text.PlainText
    text: "Apps that count as a call"
    color: Qt.darker(page.store.foreground, 1.4)
    font.family: page.store.fontFamily
    font.pixelSize: Style.font.caption
    font.bold: true
  }

  Flow {
    width: parent.width
    spacing: Style.spacing.md

    Repeater {
      model: Model.callAppsList(page.store.value("call_apps", {}))

      delegate: Button {
        required property var modelData
        text: modelData.label + "  ×"
        tooltipText: "Remove " + modelData.key
        bordered: true
        foreground: page.store.foreground
        fontFamily: page.store.fontFamily
        fontSize: Style.font.caption
        onClicked: page.store.setKey("call_apps",
          Model.callAppsRemove(page.store.value("call_apps", {}), modelData.key))
      }
    }
  }

  Row {
    width: parent.width
    spacing: Style.spacing.md

    function addApp() {
      var name = addField.text.trim()
      if (!name) return
      page.store.setKey("call_apps", Model.callAppsAdd(page.store.value("call_apps", {}), name))
      addField.text = ""
    }

    TextField {
      id: addField
      width: Style.space(220)
      placeholderText: "Process name, e.g. jitsi"
      enabled: !page.store.loadFailed
      foreground: page.store.foreground
      font.family: page.store.fontFamily
      font.pixelSize: Style.font.body
      onAccepted: parent.addApp()
    }

    Button {
      anchors.verticalCenter: parent.verticalCenter
      text: "Add"
      bordered: true
      enabled: !page.store.loadFailed && addField.text.trim() !== ""
      opacity: enabled ? 1.0 : 0.5
      foreground: page.store.foreground
      fontFamily: page.store.fontFamily
      fontSize: Style.font.caption
      onClicked: parent.addApp()
    }

    Button {
      anchors.verticalCenter: parent.verticalCenter
      text: "Reset to defaults"
      foreground: Qt.darker(page.store.foreground, 1.3)
      fontFamily: page.store.fontFamily
      fontSize: Style.font.caption
      onClicked: page.store.unsetKey("call_apps")
    }
  }
  NoteText {
    store: page.store
    text: "Matched case-insensitively against the process name and app name PipeWire reports. Run `pactl list source-outputs` during a call to find a missing one."
  }
  ErrorNote { store: page.store; key: "call_apps" }

  // ---- who's on the mic ----
  Item {
    width: parent.width
    height: Math.max(presentLine.implicitHeight, refreshButton.implicitHeight)

    Text {
      id: presentLine
      textFormat: Text.PlainText
      anchors.left: parent.left
      anchors.right: refreshButton.left
      anchors.rightMargin: Style.space(10)
      anchors.verticalCenter: parent.verticalCenter
      text: page.store.presentApps === null ? "Checking the microphone…" : Model.presentAppsLine(page.store.presentApps)
      color: Qt.darker(page.store.foreground, 1.2)
      wrapMode: Text.WordWrap
      font.family: page.store.fontFamily
      font.pixelSize: Style.font.bodySmall
    }

    Button {
      id: refreshButton
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      text: "Check again"
      bordered: true
      foreground: page.store.foreground
      fontFamily: page.store.fontFamily
      fontSize: Style.font.caption
      onClicked: page.store.loadPresentApps()
    }
  }
}
