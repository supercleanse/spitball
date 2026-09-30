pragma ComponentBehavior: Bound
import QtQuick
import qs.Commons
import "../Model.js" as Model

// The overlay's left column: one row per section from
// Model.settingsSections(), the current one painted with the theme's
// selected fill, hover with the hover fill (the same tokens the kit's
// Button and the Omarchy menu rows use). A trailing badge ("Set up" on
// Transcription while the bar shows its gear, "Soon" on a placeholder page)
// comes from Model.settingsSectionBadge. A key hint sits at the bottom.
Item {
  id: root

  required property var store
  required property var sections
  property int currentIndex: 0
  signal selected(int index)

  // Wide enough for "Transcription" plus its "Set up" badge at the default
  // 12 px base without eliding; Style.space scales it with the theme.
  implicitWidth: Style.space(172)

  Column {
    id: list
    anchors.top: parent.top
    anchors.left: parent.left
    anchors.right: parent.right
    spacing: Style.space(2)

    Repeater {
      model: root.sections

      delegate: NavRow {
        required property var modelData
        required property int index
        width: list.width
        label: modelData.label
        badge: Model.settingsSectionBadge(modelData.id, root.store.st, root.store.modelSwitch)
        current: index === root.currentIndex
        onClicked: root.selected(index)
      }
    }
  }

  Text {
    textFormat: Text.PlainText
    anchors.left: parent.left
    anchors.right: parent.right
    anchors.bottom: parent.bottom
    anchors.leftMargin: Style.spacing.controlPaddingX
    text: "j/k · 1–9 · Esc"
    color: Qt.darker(root.store.foreground, 1.6)
    font.family: root.store.fontFamily
    font.pixelSize: Style.font.caption
    elide: Text.ElideRight
  }

  component NavRow: Rectangle {
    id: row

    property string label: ""
    property string badge: ""
    property bool current: false
    signal clicked()

    readonly property bool hot: mouse.containsMouse
    readonly property color fg: root.store.foreground

    height: Style.space(30)
    radius: Style.cornerRadius
    color: row.current ? Style.selectedFillFor(row.fg, Color.accent)
      : row.hot ? Style.hoverFillFor(row.fg, Color.accent)
      : "transparent"

    Behavior on color { ColorAnimation { duration: 100 } }

    Text {
      textFormat: Text.PlainText
      anchors.left: parent.left
      anchors.right: badgeText.visible ? badgeText.left : parent.right
      anchors.leftMargin: Style.spacing.controlPaddingX
      anchors.rightMargin: Style.spacing.md
      anchors.verticalCenter: parent.verticalCenter
      text: row.label
      color: row.current ? Style.selectedStateColor(row.fg, Color.accent) : row.fg
      opacity: row.current || row.hot ? 1.0 : 0.8
      font.family: root.store.fontFamily
      font.pixelSize: Style.font.body
      font.bold: row.current
      elide: Text.ElideRight
    }

    Text {
      id: badgeText
      textFormat: Text.PlainText
      visible: row.badge !== ""
      anchors.right: parent.right
      anchors.rightMargin: Style.spacing.controlPaddingX
      anchors.verticalCenter: parent.verticalCenter
      text: row.badge
      color: row.badge === "Set up" ? "#d9a441" : Qt.darker(row.fg, 1.6)
      font.family: root.store.fontFamily
      font.pixelSize: Style.font.caption
    }

    MouseArea {
      id: mouse
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onClicked: row.clicked()
    }
  }
}
