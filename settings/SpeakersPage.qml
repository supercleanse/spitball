import QtQuick
import qs.Commons
import qs.Ui
import "../Model.js" as Model

// Speakers (docs/SPEC-v2.md section 4): naming the far side from the
// calendar's invitees (speaker_names), the on-device split of the far
// channel into separate voices (speaker_split, with whether the add-on is
// installed and a button to install it), the cap on far-side voices
// (speaker_max), and a plain statement of which providers split speakers.
SettingsPage {
  id: page
  title: "Speakers"

  Component.onCompleted: page.store.loadDiarizeStatus()

  ToggleRow {
    store: page.store
    label: "Name speakers from the calendar invite"
    checked: !!page.store.value("speaker_names", true)
    disabledLook: page.store.loadFailed
    onToggled: page.store.setKey("speaker_names", !checked)
  }
  NoteText {
    store: page.store
    text: "When a call matched a meeting, one short request to the summary endpoint matches each \"Speaker N\" to an invitee from what people say (\"thanks, Priya\", \"this is Alex\"). A sure match shows the name; an unsure one reads \"Speaker 2 (probably Priya)\"; no evidence keeps \"Speaker 2\". A call with one other invitee is named without any request. Fix a name with: spitball speakers <call-dir> <n> \"Name\"."
  }
  ErrorNote { store: page.store; key: "speaker_names" }

  PanelSeparator { foreground: page.store.foreground }

  ToggleRow {
    store: page.store
    label: "Tell far-side voices apart on this computer"
    checked: !!page.store.value("speaker_split", true)
    disabledLook: page.store.loadFailed
    onToggled: page.store.setKey("speaker_split", !checked)
  }
  NoteText {
    store: page.store
    text: "For the local provider: the other side's channel is split into separate voices before it is transcribed. Skipped when the invite lists exactly one other person; any trouble quietly keeps one \"Them\"."
  }
  ErrorNote { store: page.store; key: "speaker_split" }

  Item {
    width: parent.width
    height: Math.max(diarizeLine.implicitHeight, installDiarize.implicitHeight)

    Text {
      id: diarizeLine
      textFormat: Text.PlainText
      anchors.left: parent.left
      anchors.right: installDiarize.left
      anchors.rightMargin: Style.space(10)
      anchors.verticalCenter: parent.verticalCenter
      text: page.store.diarizeStatusFailed ? "Couldn't check the speaker split."
        : (page.store.diarizeStatus === null ? "Checking the speaker split…" : Model.diarizeStatusLine(page.store.diarizeStatus))
      color: page.store.diarizeStatusFailed ? Color.urgent : page.store.foreground
      wrapMode: Text.WordWrap
      font.family: page.store.fontFamily
      font.pixelSize: Style.font.body
    }

    Button {
      id: installDiarize
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      visible: page.store.diarizeStatus !== null && !page.store.diarizeStatus.installed
      text: "Install"
      bordered: true
      foreground: page.store.foreground
      fontFamily: page.store.fontFamily
      fontSize: Style.font.caption
      onClicked: page.store.installDiarize()
    }
  }
  NoteText {
    store: page.store
    text: "Installing adds sherpa-onnx (about 100 MB) to the live-engine virtualenv under ~/.local/share/spitball/live-engine/ and downloads two small models (about 30 MB) from the k2-fsa GitHub releases. No account or token is needed."
  }

  PanelSeparator { foreground: page.store.foreground }

  NumberRow {
    store: page.store
    label: "Most far-side voices to show"
    value: Model.speakerMax(page.store.value("speaker_max", 6))
    from: 1; to: 12
    disabledLook: page.store.loadFailed
    onCommitted: function(v) { page.store.setKey("speaker_max", v) }
  }
  ErrorNote { store: page.store; key: "speaker_max" }
  NoteText {
    store: page.store
    text: "The invite's headcount is used when known, capped here. Extra voices, and any voice with only a few words, fold into the voice speaking around them. 1 shows everyone as \"Them\"."
  }

  PanelSeparator { foreground: page.store.foreground }

  Text {
    textFormat: Text.PlainText
    text: "Which providers split speakers"
    color: Qt.darker(page.store.foreground, 1.4)
    font.family: page.store.fontFamily
    font.pixelSize: Style.font.caption
    font.bold: true
  }

  NoteText {
    store: page.store
    text: "Your channel is always labeled with your name (General). Deepgram tells far-side voices apart on its own. The local provider (voxtype) hears the far side as one voice unless the split above is installed and on. Naming works with either."
  }
}
