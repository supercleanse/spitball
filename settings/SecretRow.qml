import QtQuick
import qs.Commons
import qs.Ui

// A masked secret with a Save button. The stored value is never shown or
// loaded back -- only a placeholder saying where it comes from
// (Model.keySourcePlaceholder over `config get --json`'s {set, source}).
// Saving hands the plaintext to the store, which pipes it to
// `config set-secret` over stdin, then clears the field.
Column {
  id: root

  required property var store
  property string label: ""
  property string placeholder: "Not set"
  property bool disabledLook: false
  signal saved(string text)

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

  Row {
    width: parent.width
    spacing: Style.spacing.md

    TextField {
      id: secretField
      width: parent.width - saveBtn.width - parent.spacing
      password: true
      enabled: !root.disabledLook
      opacity: enabled ? 1.0 : 0.5
      placeholderText: root.placeholder
      foreground: root.store.foreground
      font.family: root.store.fontFamily
      font.pixelSize: Style.font.body
      onAccepted: {
        if (text !== "") { root.saved(text); text = "" }
      }
    }

    Button {
      id: saveBtn
      anchors.verticalCenter: parent.verticalCenter
      text: "Save"
      bordered: true
      enabled: !root.disabledLook && secretField.text !== ""
      opacity: enabled ? 1.0 : 0.5
      foreground: root.store.foreground
      fontFamily: root.store.fontFamily
      fontSize: Style.font.caption
      onClicked: { root.saved(secretField.text); secretField.text = "" }
    }
  }
}
