import QtQuick
import qs.Commons

// The placeholder block for a page a later phase fills in (Audio, Speakers,
// Calendar -- docs/SPEC-v2.md sections 2-4). Drop it, and the section's
// `placeholder` flag in Model.js, once the page has real controls.
Rectangle {
  id: root

  required property var store
  property string text: ""

  width: parent ? parent.width : implicitWidth
  implicitHeight: body.implicitHeight + Style.space(24)
  radius: Style.cornerRadius
  color: Qt.rgba(root.store.foreground.r, root.store.foreground.g, root.store.foreground.b, 0.05)

  Column {
    id: body
    anchors.left: parent.left
    anchors.right: parent.right
    anchors.verticalCenter: parent.verticalCenter
    anchors.margins: Style.space(12)
    spacing: Style.space(4)

    Text {
      textFormat: Text.PlainText
      text: "Coming in this release"
      color: root.store.foreground
      font.family: root.store.fontFamily
      font.pixelSize: Style.font.body
      font.bold: true
    }

    Text {
      textFormat: Text.PlainText
      visible: root.text !== ""
      width: parent.width
      text: root.text
      color: Qt.darker(root.store.foreground, 1.4)
      wrapMode: Text.WordWrap
      font.family: root.store.fontFamily
      font.pixelSize: Style.font.bodySmall
    }
  }
}
