import QtQuick
import qs.Commons

// The transient "couldn't save that one" line under a control. Visible only
// while the store's errorKey names this row's key (SettingsStore.flashError
// clears it after a few seconds), so exactly one row ever shows the message.
Text {
  id: root

  required property var store
  property string key: ""

  textFormat: Text.PlainText
  visible: root.store.errorKey === root.key && root.store.errorText !== ""
  width: parent ? parent.width : implicitWidth
  text: root.store.errorText
  color: Color.urgent
  wrapMode: Text.WordWrap
  font.family: root.store.fontFamily
  font.pixelSize: Style.font.caption
}
