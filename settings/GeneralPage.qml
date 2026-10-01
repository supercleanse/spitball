import QtQuick
import qs.Commons
import qs.Ui
import "../Model.js" as Model

// General: your name, the spoken language, auto-record (with its consent
// line), and the daemon itself.
SettingsPage {
  id: page
  title: "General"

  FieldRow {
    store: page.store
    label: "Your name"
    text: page.store.value("my_name", "")
    disabledLook: page.store.loadFailed
    onCommitted: function(t) { page.store.setKey("my_name", t) }
  }
  NoteText { store: page.store; text: "The label for your side of every transcript and summary." }
  ErrorNote { store: page.store; key: "my_name" }

  // Applies to every transcription provider, so it lives here rather than
  // inside either provider's block on the Transcription page.
  Column {
    width: parent.width
    spacing: Style.spacing.labelGap

    Text {
      textFormat: Text.PlainText
      text: "Language"
      color: Qt.darker(page.store.foreground, 1.4)
      font.family: page.store.fontFamily
      font.pixelSize: Style.font.caption
      font.bold: true
    }

    ButtonGroup {
      width: parent.width
      options: Model.languageOptions()
      value: page.store.languageChoice
      foreground: page.store.foreground
      background: Color.popups.background
      fontFamily: page.store.fontFamily
      fontSize: Style.font.bodySmall
      onChanged: function(v) {
        if (v === "en" || v === "auto") page.store.setKey("language", v)
        // "other": just reveals the code field below; nothing is saved
        // until the user actually types a code and commits it.
      }
    }
  }

  FieldRow {
    store: page.store
    visible: page.store.languageChoice === "other"
    label: "ISO code (e.g. es, fr, de)"
    text: page.store.languageChoice === "other" ? String(page.store.value("language", "")) : ""
    fieldWidth: Style.space(160)
    disabledLook: page.store.loadFailed
    onCommitted: function(t) { page.store.setKey("language", t.trim() || "auto") }
  }
  ErrorNote { store: page.store; key: "language" }

  PanelSeparator { foreground: page.store.foreground }

  // The setting most likely to surprise someone (and the one with the real
  // consent implication), so it carries its own explanation.
  ToggleRow {
    store: page.store
    label: "Record automatically when a call starts"
    checked: !!page.store.value("auto_record", false)
    disabledLook: page.store.loadFailed
    onToggled: page.store.setKey("auto_record", !checked)
  }
  NoteText { store: page.store; text: "Every detected call is recorded without asking. Tell people you're recording." }
  ErrorNote { store: page.store; key: "auto_record" }

  PanelSeparator { foreground: page.store.foreground }

  Item {
    width: parent.width
    height: Math.max(daemonLine.implicitHeight, restartButton.implicitHeight)

    Text {
      id: daemonLine
      textFormat: Text.PlainText
      anchors.left: parent.left
      anchors.right: restartButton.left
      anchors.rightMargin: Style.space(10)
      anchors.verticalCenter: parent.verticalCenter
      text: Model.daemonStateLabel(page.store.st)
      color: page.store.foreground
      elide: Text.ElideRight
      font.family: page.store.fontFamily
      font.pixelSize: Style.font.body
    }

    Button {
      id: restartButton
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      text: "Restart daemon"
      bordered: true
      foreground: page.store.foreground
      fontFamily: page.store.fontFamily
      fontSize: Style.font.caption
      onClicked: page.store.restartDaemon()
    }
  }
  NoteText { store: page.store; text: "Restarting stops any recording in progress. Use it if the daemon is stuck." }
}
