import QtQuick
import qs.Commons
import qs.Ui

// The base every settings page extends: a Column with the page's title as
// a section header, an optional one-line description, and the shared
// "Couldn't load settings" warning. A page is just `SettingsPage { title:
// "…"; <rows> }` -- rows declared in the page file append after the header.
//
// `store` is the one SettingsStore the whole overlay shares (settings dict,
// CLI round trips, error flashes); every row takes it explicitly.
Column {
  id: root

  required property var store
  property string title: ""
  property string description: ""

  width: parent ? parent.width : implicitWidth
  spacing: Style.space(12)

  PanelSectionHeader {
    text: root.title.toUpperCase()
    foreground: root.store.foreground
    fontFamily: root.store.fontFamily
  }

  NoteText {
    visible: root.description !== ""
    store: root.store
    text: root.description
  }

  NoteText {
    visible: root.store.loadFailed
    store: root.store
    urgent: true
    text: "Couldn't load settings. Spitball's config commands may not be available yet."
  }
}
