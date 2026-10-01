import QtQuick
import qs.Commons
import qs.Ui
import "../Model.js" as Model

// Audio (docs/SPEC-v2.md section 3): mic noise reduction for the
// transcriber (mic_denoise off/auto/on, one line on each), what Spitball
// records right now (read-only, straight from pactl's defaults -- the first
// thing to check when one side of a call comes out silent), and under
// Advanced the noise-floor threshold the Auto mode gates on.
SettingsPage {
  id: page
  title: "Audio"

  readonly property string denoiseMode: Model.micDenoiseChoice(page.store.value("mic_denoise", "auto"))

  Component.onCompleted: page.store.loadAudioDevices()

  Text {
    textFormat: Text.PlainText
    text: "Mic noise reduction"
    color: Qt.darker(page.store.foreground, 1.4)
    font.family: page.store.fontFamily
    font.pixelSize: Style.font.caption
    font.bold: true
  }

  ButtonGroup {
    width: parent.width
    options: Model.micDenoiseOptions()
    value: page.denoiseMode
    foreground: page.store.foreground
    background: Color.popups.background
    fontFamily: page.store.fontFamily
    onChanged: function(v) { page.store.setKey("mic_denoise", v) }
  }
  ErrorNote { store: page.store; key: "mic_denoise" }

  // One line per mode, the selected one at full strength.
  Column {
    width: parent.width
    spacing: Style.space(4)

    Repeater {
      model: Model.micDenoiseNotes(page.denoiseMode)

      Text {
        required property var modelData
        textFormat: Text.PlainText
        width: parent ? parent.width : implicitWidth
        text: modelData.label + " — " + modelData.note
        color: modelData.selected ? page.store.foreground : Qt.darker(page.store.foreground, 1.4)
        wrapMode: Text.WordWrap
        font.family: page.store.fontFamily
        font.pixelSize: Style.font.caption
      }
    }
  }

  NoteText {
    store: page.store
    text: "Only the copy of your mic channel that the transcriber hears is changed. The recording itself and the other side's channel are never touched, and each transcript records whether noise reduction ran."
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
    text: "Spitball follows your default devices: your side comes from the microphone, theirs from the monitor of whatever is playing. If their side is missing, the default sink is probably a dock or headset you've unplugged. Echo from laptop speakers is best fixed at the source with PipeWire's echo cancellation (see the README)."
  }

  PanelSeparator { foreground: page.store.foreground }

  RevealRow {
    store: page.store

    NumberRow {
      store: page.store
      label: "Auto denoises when the mic's background is louder than"
      value: Model.micNoiseFloorDb(page.store.value("mic_noise_floor_db", -45))
      from: -80; to: -20; unit: "dBFS"
      disabledLook: page.store.loadFailed || page.denoiseMode !== "auto"
      onCommitted: function(v) { page.store.setKey("mic_noise_floor_db", v) }
    }
    ErrorNote { store: page.store; key: "mic_noise_floor_db" }
    NoteText {
      store: page.store
      text: "The background level is the quietest tenth of the call's mic audio. A headset in a quiet room sits near -60; a laptop fan around -45 to -40; a cafe above -35. Lower the number to denoise more often."
    }
  }
}
