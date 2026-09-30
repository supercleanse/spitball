import QtQuick
import qs.Commons
import qs.Ui
import "../Model.js" as Model

// Summary: on/off, the OpenAI-compatible endpoint, the model (a dropdown
// when the endpoint lists any, a text field otherwise), the key, a Test
// button, and the key command under Advanced.
SettingsPage {
  id: page
  title: "Summary"

  ToggleRow {
    store: page.store
    label: "Summarize calls"
    checked: !!page.store.value("summary_enabled", true)
    disabledLook: page.store.loadFailed
    onToggled: page.store.setKey("summary_enabled", !checked)
  }
  NoteText { store: page.store; text: "Off keeps the transcript only. `spitball reprocess <dir>` can add a summary later." }
  ErrorNote { store: page.store; key: "summary_enabled" }

  Column {
    visible: !!page.store.value("summary_enabled", true)
    width: parent.width
    spacing: Style.space(10)

    FieldRow {
      store: page.store
      label: "Endpoint URL"
      text: page.store.value("summary_base_url", "")
      placeholder: "http://127.0.0.1:11434/v1"
      disabledLook: page.store.loadFailed
      onCommitted: function(t) { page.store.setKey("summary_base_url", t) }
    }
    NoteText { store: page.store; text: "Any OpenAI-compatible chat endpoint: a local Ollama by default, or LM Studio, OpenAI, OpenRouter." }
    ErrorNote { store: page.store; key: "summary_base_url" }

    Dropdown {
      visible: page.store.summaryModels.length > 0
      width: parent.width
      label: "Model"
      options: page.store.summaryModels.map(function(m) { return typeof m === "object" ? m : { value: String(m), label: String(m) } })
      value: String(page.store.value("summary_model", ""))
      foreground: page.store.foreground
      fontFamily: page.store.fontFamily
      onChanged: function(v) { page.store.setKey("summary_model", v) }
    }

    FieldRow {
      store: page.store
      visible: page.store.summaryModels.length === 0
      label: "Model"
      text: page.store.value("summary_model", "")
      placeholder: "Empty means the first model the endpoint lists"
      disabledLook: page.store.loadFailed
      onCommitted: function(t) { page.store.setKey("summary_model", t) }
    }
    ErrorNote { store: page.store; key: "summary_model" }

    SecretRow {
      store: page.store
      label: "API key"
      placeholder: Model.keySourcePlaceholder(page.store.secretInfo("summary_api_key"))
      disabledLook: page.store.loadFailed
      onSaved: function(t) { page.store.setSecret("summary_api_key", t) }
    }
    ErrorNote { store: page.store; key: "summary_api_key" }

    TestRow {
      store: page.store
      busy: page.store.summaryTesting
      message: page.store.summaryTestMsg
      disabledLook: page.store.loadFailed
      onRun: page.store.testSummary()
    }

    RevealRow {
      store: page.store

      FieldRow {
        store: page.store
        label: "Key command"
        text: page.store.value("summary_api_key_command", "")
        placeholder: "e.g. op read op://Private/OpenRouter/credential"
        disabledLook: page.store.loadFailed
        onCommitted: function(t) { page.store.setKey("summary_api_key_command", t) }
      }
      NoteText { store: page.store; text: "A shell command whose output is the key, for a password manager." }
      ErrorNote { store: page.store; key: "summary_api_key_command" }
    }
  }
}
