import QtQuick
import qs.Commons
import qs.Ui
import "../Model.js" as Model

// Transcription: the provider choice, then that provider's block. This is
// the page every "Set up transcription…" prompt opens, since the
// setup_needed reason (missing key / no local model / voxtype missing)
// always gets resolved here.
//
// Phase 1 of docs/SPEC-settings-and-providers.md section 8: exactly two
// choices, Local (voxtype) / Deepgram, Local first as the default.
// OpenAI-compatible/AssemblyAI/Soniox are still parked -- see
// docs/ROADMAP.md and docs/phase2/.
SettingsPage {
  id: page
  title: "Transcription"

  Component.onCompleted: {
    page.store.loadLocalInfo()
    page.store.loadLocalModels()
  }

  ButtonGroup {
    width: parent.width
    options: Model.providerOptions()
    value: page.store.provider
    foreground: page.store.foreground
    background: Color.popups.background
    fontFamily: page.store.fontFamily
    onChanged: function(v) { page.store.setKey("transcription_provider", v) }
  }
  ErrorNote { store: page.store; key: "transcription_provider" }

  // ---- Local (voxtype) ----
  Column {
    visible: page.store.provider === "local"
    width: parent.width
    spacing: Style.space(8)

    NoteText {
      store: page.store
      visible: page.store.localInfoFailed
      urgent: true
      text: "Couldn't check voxtype."
    }

    Text {
      textFormat: Text.PlainText
      visible: !page.store.localInfoFailed && page.store.localInfo.installed
      width: parent.width
      text: Model.localInfoLine(page.store.localInfo)
      color: page.store.foreground
      font.family: page.store.fontFamily
      font.pixelSize: Style.font.body
    }

    // ---- model picker ----
    // Picking a different model doesn't switch anything by itself -- it
    // reveals the inline confirmation row below, which is the only thing
    // that actually calls `local set-model`.
    Column {
      visible: !page.store.localInfoFailed && page.store.localInfo.installed
      width: parent.width
      spacing: Style.space(6)

      NoteText {
        store: page.store
        visible: page.store.localModelsFailed
        urgent: true
        text: "Couldn't list voxtype models."
      }

      Dropdown {
        visible: !page.store.localModelsFailed && page.store.localModels.length > 0
        width: parent.width
        label: "Model"
        options: Model.localModelOptions(page.store.localModels)
        value: page.store.pendingModelName
        foreground: page.store.foreground
        fontFamily: page.store.fontFamily
        opacity: page.store.modelSwitchBusy ? 0.5 : 1.0
        // Disabled (in effect) while a switch is already running -- there's
        // nothing sane to confirm on top of one already in flight.
        onChanged: function(v) {
          if (page.store.modelSwitchBusy) return
          page.store.pendingModelName = v
          page.store.pendingModelDirty = true
        }
      }

      // ---- inline confirmation ----
      Column {
        visible: !page.store.localModelsFailed && page.store.localModels.length > 0 && !page.store.modelSwitchBusy
          && page.store.pendingModelName !== Model.activeModelName(page.store.localModels)
        width: parent.width
        spacing: Style.space(6)

        Text {
          textFormat: Text.PlainText
          width: parent.width
          text: Model.switchConfirmText(page.store.localModels, page.store.pendingModelName)
          color: page.store.foreground
          wrapMode: Text.WordWrap
          font.family: page.store.fontFamily
          font.pixelSize: Style.font.bodySmall
        }

        Row {
          spacing: Style.space(8)

          Button {
            text: "Switch"
            bordered: true
            selected: true
            foreground: page.store.foreground
            fontFamily: page.store.fontFamily
            fontSize: Style.font.caption
            onClicked: page.store.confirmSwitchModel()
          }

          Button {
            text: "Cancel"
            bordered: true
            foreground: page.store.foreground
            fontFamily: page.store.fontFamily
            fontSize: Style.font.caption
            onClicked: page.store.cancelSwitchModel()
          }
        }
      }

      // ---- in-flight / just-failed switch status ----
      // Driven by model.json, the same file Widget.qml's bar dot watches,
      // so this and the bar agree. A "downloading" step gets a real bar;
      // engine-switch/activate/restart steps have no byte count to show.
      Column {
        visible: page.store.modelSwitch !== null
          && (Model.modelSwitchActive(page.store.modelSwitch) || Model.modelSwitchFailed(page.store.modelSwitch))
        width: parent.width
        spacing: Style.space(4)

        Text {
          textFormat: Text.PlainText
          width: parent.width
          text: Model.modelSwitchStatusText(page.store.modelSwitch)
          color: Model.modelSwitchFailed(page.store.modelSwitch) ? Color.urgent : page.store.foreground
          wrapMode: Text.WordWrap
          font.family: page.store.fontFamily
          font.pixelSize: Style.font.bodySmall
        }

        Item {
          visible: page.store.modelSwitch !== null && page.store.modelSwitch.state === "downloading"
            && Model.modelSwitchPercent(page.store.modelSwitch) >= 0
          width: parent.width
          height: Style.space(6)

          Rectangle {
            anchors.fill: parent
            radius: height / 2
            color: Qt.rgba(page.store.foreground.r, page.store.foreground.g, page.store.foreground.b, 0.15)
          }
          Rectangle {
            anchors.left: parent.left
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            radius: height / 2
            color: page.store.foreground
            width: parent.width * Math.max(0, Model.modelSwitchPercent(page.store.modelSwitch)) / 100
          }
        }
      }
    }

    NoteText {
      store: page.store
      visible: !page.store.localInfoFailed && !page.store.localInfo.installed
      text: "Install Omarchy's dictation (voxtype) to transcribe on this computer."
    }

    Button {
      visible: !page.store.localInfoFailed && !page.store.localInfo.installed
      text: "Install"
      bordered: true
      foreground: page.store.foreground
      fontFamily: page.store.fontFamily
      fontSize: Style.font.caption
      onClicked: page.store.installVoxtype()
    }

    NoteText {
      store: page.store
      text: "Runs on this computer. Slower than cloud; audio never leaves your machine."
    }
  }

  // ---- Deepgram ----
  Column {
    visible: page.store.provider === "deepgram"
    width: parent.width
    spacing: Style.space(10)

    SecretRow {
      store: page.store
      label: "API key"
      placeholder: Model.keySourcePlaceholder(page.store.secretInfo("deepgram_api_key"))
      disabledLook: page.store.loadFailed
      onSaved: function(t) { page.store.setSecret("deepgram_api_key", t) }
    }
    ErrorNote { store: page.store; key: "deepgram_api_key" }

    FieldRow {
      store: page.store
      label: "Model"
      text: page.store.value("deepgram_model", "nova-3")
      fieldWidth: Style.space(200)
      disabledLook: page.store.loadFailed
      onCommitted: function(t) { page.store.setKey("deepgram_model", t) }
    }
    ErrorNote { store: page.store; key: "deepgram_model" }

    TestRow {
      store: page.store
      busy: page.store.deepgramTesting
      message: page.store.deepgramTestMsg
      disabledLook: page.store.loadFailed
      onRun: page.store.testTranscription("deepgram")
    }

    RevealRow {
      store: page.store

      FieldRow {
        store: page.store
        label: "Key command"
        text: page.store.value("deepgram_api_key_command", "")
        placeholder: "e.g. op read op://Private/Deepgram/credential"
        disabledLook: page.store.loadFailed
        onCommitted: function(t) { page.store.setKey("deepgram_api_key_command", t) }
      }
      NoteText {
        store: page.store
        text: "A shell command whose output is the key, for a password manager. The DEEPGRAM_API_KEY environment variable wins over both."
      }
      ErrorNote { store: page.store; key: "deepgram_api_key_command" }
    }

    NoteText {
      store: page.store
      text: "Cloud transcription, multichannel and diarized, so the far side can have several speakers. The recording is uploaded once, with model-improvement opted out."
    }
  }
}
