import QtQuick
import QtQuick.Controls
import qs.Commons
import qs.Ui
import "../Model.js" as Model

// The settings overlay's card content: a pinned header (title, daemon
// status, ✕), the section nav on the left, and the current page on the
// right in a scrolling viewport. Owns nothing about the window chrome
// (SettingsWindow.qml does the scrim, focus, and dismissal) so it can also
// be rendered standalone inside a plain window -- tests/offscreen does
// exactly that.
//
// Keys, per docs/SPEC-v2.md section 1: while nothing on the page has focus,
// j/k and Up/Down move between sections, 1-9 jump, Tab walks into the
// page's controls, Esc closes. Once a control (text field, number field,
// dropdown, chip group) has focus, keys go to it; Esc there hands focus
// back to the section list instead of closing.
Item {
  id: root

  required property var store
  property string section: Model.settingsDefaultSection()
  signal closeRequested()

  readonly property var sections: Model.settingsSections()
  readonly property int sectionIndex: Math.max(0, Model.settingsSectionIndex(root.section))

  function showSection(id) {
    var i = Model.settingsSectionIndex(id)
    if (i < 0) i = 0
    root.section = root.sections[i].id
  }

  function stepSection(delta) {
    root.section = root.sections[Model.settingsNavStep(root.sectionIndex, delta)].id
  }

  // Puts keyboard focus on the key catcher -- the "nothing on the page has
  // focus" state where j/k and digits drive the nav.
  function focusKeys() {
    keyCatcher.forceActiveFocus()
  }

  // Whatever item currently holds the window's active focus. The catcher
  // is blocked (keys pass through untouched) whenever that's anything but
  // itself -- a text field mid-edit, a spin box, a dropdown trigger or its
  // open popup's list, a chip group walked to with Tab.
  readonly property var focusItem: root.Window.window ? root.Window.window.activeFocusItem : null
  readonly property bool controlHasFocus: !!root.focusItem && root.focusItem !== keyCatcher
  readonly property bool editorHasFocus: root.controlHasFocus && root.focusItem.cursorPosition !== undefined

  // Esc reaching this level means the catcher was blocked (a control has
  // focus) and the control didn't want it: an editor drops back to the
  // section list; any other control closes like the catcher would.
  Keys.onEscapePressed: function(event) {
    if (root.editorHasFocus) root.focusKeys()
    else root.closeRequested()
    event.accepted = true
  }

  // Resolves the index from `section` directly rather than reading
  // `sectionIndex`: a change handler can run before a dependent binding has
  // re-evaluated, which would load the page one section behind the nav.
  function loadPage() {
    var i = Math.max(0, Model.settingsSectionIndex(root.section))
    flick.contentY = 0
    pageLoader.setSource(Qt.resolvedUrl(root.sections[i].page), { store: root.store })
  }

  onSectionChanged: root.loadPage()
  Component.onCompleted: root.loadPage()

  PanelKeyCatcher {
    id: keyCatcher
    anchors.fill: parent
    blocked: root.controlHasFocus

    onCloseRequested: root.closeRequested()
    onMoveRequested: function(dx, dy) { if (dy !== 0) root.stepSection(dy) }
    onTabRequested: function(direction) {
      var next = keyCatcher.nextItemInFocusChain(direction > 0)
      if (next && next !== keyCatcher) next.forceActiveFocus()
    }
    onTextKey: function(text) {
      var i = Model.settingsSectionForKey(text)
      if (i >= 0) root.section = root.sections[i].id
    }

    Column {
      id: layout
      anchors.fill: parent
      spacing: Style.space(10)

      // ---- pinned header ----
      Item {
        id: header
        width: parent.width
        height: Math.max(titleText.implicitHeight, closeButton.implicitHeight)

        Text {
          id: titleText
          textFormat: Text.PlainText
          anchors.left: parent.left
          anchors.verticalCenter: parent.verticalCenter
          text: "Spitball settings"
          color: root.store.foreground
          font.family: root.store.fontFamily
          font.pixelSize: Style.font.title
          font.bold: true
        }

        Text {
          textFormat: Text.PlainText
          anchors.right: closeButton.left
          anchors.rightMargin: Style.space(12)
          anchors.verticalCenter: parent.verticalCenter
          text: Model.daemonStateLabel(root.store.st)
          color: Qt.darker(root.store.foreground, 1.5)
          font.family: root.store.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }

        Button {
          id: closeButton
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          text: "✕"
          tooltipText: "Close (Esc)"
          foreground: root.store.foreground
          fontFamily: root.store.fontFamily
          fontSize: Style.font.caption
          onClicked: root.closeRequested()
        }
      }

      PanelSeparator { id: headerSeparator; width: parent.width; foreground: root.store.foreground }

      // ---- nav + page ----
      Item {
        width: parent.width
        height: layout.height - header.height - headerSeparator.height - layout.spacing * 2

        SettingsNav {
          id: nav
          anchors.top: parent.top
          anchors.bottom: parent.bottom
          anchors.left: parent.left
          width: implicitWidth
          store: root.store
          sections: root.sections
          currentIndex: root.sectionIndex
          onSelected: function(index) {
            root.section = root.sections[index].id
            root.focusKeys()
          }
        }

        Rectangle {
          id: navSeparator
          anchors.top: parent.top
          anchors.bottom: parent.bottom
          anchors.left: nav.right
          anchors.leftMargin: Style.space(10)
          width: 1
          color: Qt.rgba(root.store.foreground.r, root.store.foreground.g, root.store.foreground.b, 0.12)
        }

        Flickable {
          id: flick
          anchors.top: parent.top
          anchors.bottom: parent.bottom
          anchors.left: navSeparator.right
          anchors.leftMargin: Style.space(16)
          anchors.right: parent.right
          contentWidth: width
          contentHeight: pageLoader.item ? pageLoader.item.implicitHeight + Style.space(4) : 0
          clip: true
          boundsBehavior: Flickable.StopAtBounds
          interactive: contentHeight > height

          ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

          Loader {
            id: pageLoader
            width: flick.width
            asynchronous: false
          }
        }
      }
    }
  }
}
