import QtQuick
import qs.Commons

// Dimmed explanatory caption under a control ("Audio is never copied.",
// the auto-record consent line). Wraps to the page width.
Text {
  id: root

  required property var store
  property bool urgent: false

  textFormat: Text.PlainText
  width: parent ? parent.width : implicitWidth
  color: root.urgent ? Color.urgent : Qt.darker(root.store.foreground, 1.4)
  wrapMode: Text.WordWrap
  font.family: root.store.fontFamily
  font.pixelSize: Style.font.caption
}
