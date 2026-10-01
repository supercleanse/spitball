import QtQuick
import qs.Commons
import qs.Ui

// Label, a NumberField, and an optional unit ("s") beside it -- for the
// integer timing keys (detect_after_s, end_after_s, min_call_s,
// min_manual_s, live_max_window_s). Emits `committed(value)` on every
// spin/edit the field reports as modified.
Column {
  id: root

  required property var store
  property string label: ""
  property int value: 0
  property int from: 0
  property int to: 3600
  property int stepSize: 1
  property string unit: ""
  property bool disabledLook: false
  signal committed(int value)

  width: parent ? parent.width : implicitWidth
  spacing: Style.spacing.labelGap

  Text {
    textFormat: Text.PlainText
    visible: root.label !== ""
    width: parent.width
    text: root.label
    color: Qt.darker(root.store.foreground, 1.4)
    elide: Text.ElideRight
    font.family: root.store.fontFamily
    font.pixelSize: Style.font.caption
    font.bold: true
  }

  Row {
    spacing: Style.spacing.md

    NumberField {
      id: field
      value: root.value
      from: root.from
      to: root.to
      stepSize: root.stepSize
      enabled: !root.disabledLook
      opacity: enabled ? 1.0 : 0.5
      foreground: root.store.foreground
      fontFamily: root.store.fontFamily
      fieldWidth: Style.space(96)
      onModified: function(v) { root.committed(v) }
    }

    Text {
      textFormat: Text.PlainText
      visible: root.unit !== ""
      anchors.verticalCenter: parent.verticalCenter
      text: root.unit
      color: Qt.darker(root.store.foreground, 1.4)
      font.family: root.store.fontFamily
      font.pixelSize: Style.font.bodySmall
    }
  }
}
