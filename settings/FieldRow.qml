import QtQuick
import qs.Commons
import qs.Ui

// Label over a single-line text field. Commits on editingFinished (Enter or
// focus leaving); the store's optimistic write keeps the field showing what
// was typed even if the CLI round trip later fails (an ErrorNote says so).
Column {
  id: root

  required property var store
  property string label: ""
  property string text: ""
  property string placeholder: ""
  property bool disabledLook: false
  property real fieldWidth: -1
  signal committed(string text)

  width: parent ? parent.width : implicitWidth
  spacing: Style.spacing.labelGap

  Text {
    textFormat: Text.PlainText
    visible: root.label !== ""
    text: root.label
    color: Qt.darker(root.store.foreground, 1.4)
    font.family: root.store.fontFamily
    font.pixelSize: Style.font.caption
    font.bold: true
  }

  TextField {
    id: field
    width: root.fieldWidth > 0 ? root.fieldWidth : parent.width
    text: root.text
    placeholderText: root.placeholder
    enabled: !root.disabledLook
    opacity: enabled ? 1.0 : 0.5
    foreground: root.store.foreground
    font.family: root.store.fontFamily
    font.pixelSize: Style.font.body
    onEditingFinished: root.committed(text)

    // Keep the field in sync when the underlying value changes from
    // elsewhere (a fresh load(), an optimistic update from another
    // control) without stomping on what the user is mid-typing.
    Connections {
      target: root
      function onTextChanged() { if (!field.activeFocus) field.text = root.text }
    }
  }
}
