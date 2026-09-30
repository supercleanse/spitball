import QtQuick
import qs.Commons
import qs.Ui

// Label on the left, a compact ToggleSwitch on the right. Stateless about
// the value: bind `checked` to the store and flip it in `toggled()`.
Item {
  id: root

  required property var store
  property string label: ""
  property bool checked: false
  property bool disabledLook: false
  signal toggled()

  width: parent ? parent.width : implicitWidth
  height: Style.space(26)

  Text {
    textFormat: Text.PlainText
    anchors.left: parent.left
    anchors.right: sw.left
    anchors.rightMargin: Style.space(10)
    anchors.verticalCenter: parent.verticalCenter
    text: root.label
    color: root.store.foreground
    opacity: root.disabledLook ? 0.5 : (root.checked ? 0.9 : 0.65)
    font.family: root.store.fontFamily
    font.pixelSize: Style.font.body
    elide: Text.ElideRight
  }

  ToggleSwitch {
    id: sw
    anchors.right: parent.right
    anchors.verticalCenter: parent.verticalCenter
    checked: root.checked
    interactive: !root.disabledLook
    trackHeight: Style.space(18)
    foreground: root.store.foreground
    onToggled: root.toggled()
  }
}
