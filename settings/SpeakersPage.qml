import QtQuick
import qs.Commons
import qs.Ui

// Speakers: placeholder for speaker naming and the local far-side split
// (docs/SPEC-v2.md section 4). Until then, a plain statement of how
// speakers are labeled today.
SettingsPage {
  id: page
  title: "Speakers"

  ComingSoon {
    store: page.store
    text: "Naming the far side from your calendar's attendees, an on-device split of the far channel into separate speakers, and a `spitball speakers` command to correct names."
  }

  PanelSeparator { foreground: page.store.foreground }

  Text {
    textFormat: Text.PlainText
    text: "How speakers are labeled today"
    color: Qt.darker(page.store.foreground, 1.4)
    font.family: page.store.fontFamily
    font.pixelSize: Style.font.caption
    font.bold: true
  }

  NoteText {
    store: page.store
    text: "Your channel is labeled with your name (General). The far channel is \"Them\" with the local provider, or \"Speaker 1…N\" with Deepgram, which tells voices apart on its own."
  }
}
