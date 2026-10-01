import QtQuick
import qs.Commons
import qs.Ui

// A folder path with Change… (portal picker via the store) and an optional
// Open button. `editable` swaps the read-only path for a text field -- the
// store flips it on when `spitball pick-folder` reports no picker at all
// (exit 2), so the path can still be typed by hand.
Column {
  id: root

  required property var store
  property string label: ""
  property string path: ""
  property bool editable: false
  property bool disabledLook: false
  property bool showOpen: true
  signal changeRequested()
  signal openRequested()
  signal pathEdited(string text)

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

    readonly property real textWidth: width - changeBtn.width - (openBtn.visible ? openBtn.width + spacing : 0) - spacing

    TextField {
      id: pathField
      visible: root.editable
      width: parent.textWidth
      text: root.path
      enabled: !root.disabledLook
      foreground: root.store.foreground
      font.family: root.store.fontFamily
      font.pixelSize: Style.font.bodySmall
      onEditingFinished: root.pathEdited(text)
    }

    Text {
      textFormat: Text.PlainText
      visible: !root.editable
      width: parent.textWidth
      anchors.verticalCenter: changeBtn.verticalCenter
      text: root.path !== "" ? root.path : "(default)"
      color: root.store.foreground
      opacity: 0.8
      elide: Text.ElideMiddle
      font.family: root.store.fontFamily
      font.pixelSize: Style.font.bodySmall
    }

    Button {
      id: changeBtn
      anchors.verticalCenter: parent.verticalCenter
      text: "Change…"
      bordered: true
      enabled: !root.disabledLook
      opacity: enabled ? 1.0 : 0.5
      foreground: root.store.foreground
      fontFamily: root.store.fontFamily
      fontSize: Style.font.caption
      onClicked: root.changeRequested()
    }

    Button {
      id: openBtn
      anchors.verticalCenter: parent.verticalCenter
      visible: root.showOpen
      text: "Open"
      bordered: true
      enabled: !root.disabledLook
      opacity: enabled ? 1.0 : 0.5
      foreground: root.store.foreground
      fontFamily: root.store.fontFamily
      fontSize: Style.font.caption
      onClicked: root.openRequested()
    }
  }
}
