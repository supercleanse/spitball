import QtQuick
import qs.Commons
import qs.Ui

// Audio: placeholder for the noise work (docs/SPEC-v2.md section 3 --
// mic_denoise off/auto/on lands here). Until then it shows what Spitball
// would record right now, read straight from pactl's defaults, which is the
// first thing to check when one side of a call comes out silent.
SettingsPage {
  id: page
  title: "Audio"

  Component.onCompleted: page.store.loadAudioDevices()

  ComingSoon {
    store: page.store
    text: "Mic noise reduction (off / auto / on) for the transcriber, with the raw recording left untouched."
  }

  PanelSeparator { foreground: page.store.foreground }

  Text {
    textFormat: Text.PlainText
    text: "What Spitball records"
    color: Qt.darker(page.store.foreground, 1.4)
    font.family: page.store.fontFamily
    font.pixelSize: Style.font.caption
    font.bold: true
  }

  Text {
    textFormat: Text.PlainText
    width: parent.width
    text: "Microphone: " + (page.store.audioSource || "(default source)")
    color: page.store.foreground
    elide: Text.ElideMiddle
    font.family: page.store.fontFamily
    font.pixelSize: Style.font.bodySmall
  }

  Text {
    textFormat: Text.PlainText
    width: parent.width
    text: "Speakers: " + (page.store.audioSink || "(default sink)") + (page.store.audioSink ? ".monitor" : "")
    color: page.store.foreground
    elide: Text.ElideMiddle
    font.family: page.store.fontFamily
    font.pixelSize: Style.font.bodySmall
  }

  NoteText {
    store: page.store
    text: "Spitball follows your default devices: your side comes from the microphone, theirs from the monitor of whatever is playing. If their side is missing, the default sink is probably a dock or headset you've unplugged."
  }
}
