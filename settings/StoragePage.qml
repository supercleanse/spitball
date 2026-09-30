import QtQuick
import qs.Commons
import qs.Ui

// Storage: the optional notes copy (export_dir) and a way into the calls
// folder. The calls folder itself is chosen on the Recording page.
SettingsPage {
  id: page
  title: "Storage"

  ToggleRow {
    store: page.store
    label: "Also copy notes to a folder"
    checked: String(page.store.value("export_dir", "")) !== ""
    disabledLook: page.store.loadFailed
    onToggled: {
      if (checked) page.store.setKey("export_dir", "")
      else page.store.pickFolder("export")
    }
  }

  FolderRow {
    store: page.store
    visible: String(page.store.value("export_dir", "")) !== ""
    label: "Notes folder"
    path: page.store.value("export_dir", "")
    editable: page.store.notesDirEditable
    disabledLook: page.store.loadFailed
    showOpen: false
    onChangeRequested: page.store.pickFolder("export")
    onPathEdited: function(text) { page.store.setKey("export_dir", text) }
  }
  NoteText {
    store: page.store
    text: "Each call's summary and transcript go there as one markdown file, handy for an Obsidian vault or a notes tool. Audio is never copied."
  }
  ErrorNote { store: page.store; key: "export_dir" }

  PanelSeparator { foreground: page.store.foreground }

  Text {
    textFormat: Text.PlainText
    width: parent.width
    text: "Calls folder: " + (page.store.value("calls_dir", "") || "~/Calls")
    color: page.store.foreground
    elide: Text.ElideMiddle
    font.family: page.store.fontFamily
    font.pixelSize: Style.font.bodySmall
  }

  Button {
    text: "Open calls folder"
    bordered: true
    foreground: page.store.foreground
    fontFamily: page.store.fontFamily
    fontSize: Style.font.caption
    onClicked: page.store.openCallsFolder()
  }
  NoteText {
    store: page.store
    text: "Every call is its own folder: audio.opus, transcript.md, and summary.md. Change where they go on the Recording page. Removing the plugin never touches them."
  }
}
