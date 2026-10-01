import QtQuick
import qs.Commons
import qs.Ui
import "../Model.js" as Model

// About: version and location, a snapshot of the daemon's state, and the
// docs.
SettingsPage {
  id: page
  title: "About"

  Text {
    textFormat: Text.PlainText
    text: "Spitball" + (page.store.version ? " " + page.store.version : "")
    color: page.store.foreground
    font.family: page.store.fontFamily
    font.pixelSize: Style.font.heading
    font.bold: true
  }

  NoteText {
    store: page.store
    text: "Records your calls, transcribes both sides, and writes a summary."
  }

  Text {
    textFormat: Text.PlainText
    width: parent.width
    text: "Installed at " + page.store.pluginDir
    color: Qt.darker(page.store.foreground, 1.3)
    elide: Text.ElideMiddle
    font.family: page.store.fontFamily
    font.pixelSize: Style.font.bodySmall
  }

  PanelSeparator { foreground: page.store.foreground }

  Text {
    textFormat: Text.PlainText
    text: "Status"
    color: Qt.darker(page.store.foreground, 1.4)
    font.family: page.store.fontFamily
    font.pixelSize: Style.font.caption
    font.bold: true
  }

  Text {
    textFormat: Text.PlainText
    width: parent.width
    text: Model.daemonStateLabel(page.store.st)
    color: page.store.foreground
    wrapMode: Text.WordWrap
    font.family: page.store.fontFamily
    font.pixelSize: Style.font.bodySmall
  }

  Text {
    textFormat: Text.PlainText
    visible: !!page.store.st.setup_needed
    width: parent.width
    text: "Setup needed: " + page.store.st.setup_needed
    color: "#d9a441"
    wrapMode: Text.WordWrap
    font.family: page.store.fontFamily
    font.pixelSize: Style.font.bodySmall
  }

  Text {
    textFormat: Text.PlainText
    width: parent.width
    text: page.store.st.last_call && page.store.st.last_call.title
      ? "Last call: " + page.store.st.last_call.title
      : "No calls recorded yet."
    color: page.store.foreground
    elide: Text.ElideRight
    font.family: page.store.fontFamily
    font.pixelSize: Style.font.bodySmall
  }

  Text {
    textFormat: Text.PlainText
    width: parent.width
    text: "Auto-record: " + (page.store.st.auto_record ? "on" : "off")
      + " · Transcription: " + Model.providerLabel(page.store.provider)
      + " · Summaries: " + (page.store.value("summary_enabled", true) ? "on" : "off")
    color: Qt.darker(page.store.foreground, 1.3)
    wrapMode: Text.WordWrap
    font.family: page.store.fontFamily
    font.pixelSize: Style.font.bodySmall
  }

  PanelSeparator { foreground: page.store.foreground }

  Row {
    spacing: Style.spacing.md

    Button {
      text: "README"
      bordered: true
      foreground: page.store.foreground
      fontFamily: page.store.fontFamily
      fontSize: Style.font.caption
      onClicked: page.store.openUrl(page.store.pluginDir + "/README.md")
    }

    Button {
      text: "Contract"
      bordered: true
      foreground: page.store.foreground
      fontFamily: page.store.fontFamily
      fontSize: Style.font.caption
      onClicked: page.store.openUrl(page.store.pluginDir + "/CONTRACT.md")
    }

    Button {
      text: "GitHub"
      bordered: true
      foreground: page.store.foreground
      fontFamily: page.store.fontFamily
      fontSize: Style.font.caption
      onClicked: page.store.openUrl("https://github.com/supercleanse/spitball")
    }
  }

  NoteText { store: page.store; text: "MIT licensed. Audio never leaves your machine unless you choose Deepgram." }
}
