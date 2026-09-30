import QtQuick
import qs.Commons
import qs.Ui

// A disclosure ("Advanced ▸" / "Advanced ▾") that hides rarely-needed
// controls -- the *_api_key_command fields -- until clicked. Children
// declared inside a RevealRow land in the collapsible body.
Column {
  id: root

  required property var store
  property string label: "Advanced"
  property bool expanded: false
  default property alias body: bodyColumn.data

  width: parent ? parent.width : implicitWidth
  spacing: Style.spacing.lg

  Button {
    text: root.label + (root.expanded ? "  ▾" : "  ▸")
    leftAlign: true
    foreground: Qt.darker(root.store.foreground, 1.3)
    fontFamily: root.store.fontFamily
    fontSize: Style.font.caption
    horizontalPadding: 0
    onClicked: root.expanded = !root.expanded
  }

  Column {
    id: bodyColumn
    visible: root.expanded
    width: parent.width
    spacing: Style.spacing.lg
  }
}
