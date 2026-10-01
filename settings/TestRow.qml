import QtQuick
import qs.Commons
import qs.Ui

// A "Test" button with the result beside it: "✓ …" in the normal color,
// anything else in the urgent color. `busy` swaps the label and disables
// the button while a `spitball check …` round trip is running.
Item {
  id: root

  required property var store
  property bool busy: false
  property string message: ""
  property string label: "Test"
  property bool disabledLook: false
  signal run()

  width: parent ? parent.width : implicitWidth
  height: Math.max(testBtn.implicitHeight, resultLabel.implicitHeight)

  Button {
    id: testBtn
    text: root.busy ? "Testing…" : root.label
    bordered: true
    enabled: !root.disabledLook && !root.busy
    opacity: enabled ? 1.0 : 0.5
    foreground: root.store.foreground
    fontFamily: root.store.fontFamily
    fontSize: Style.font.caption
    onClicked: root.run()
  }

  Text {
    id: resultLabel
    textFormat: Text.PlainText
    visible: root.message !== ""
    anchors.left: testBtn.right
    anchors.leftMargin: Style.space(8)
    anchors.right: parent.right
    anchors.verticalCenter: testBtn.verticalCenter
    text: root.message
    color: root.message.indexOf("✓") === 0 ? root.store.foreground : Color.urgent
    wrapMode: Text.WordWrap
    font.family: root.store.fontFamily
    font.pixelSize: Style.font.caption
  }
}
