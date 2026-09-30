import QtQuick
import Quickshell
import Quickshell.Wayland
import qs.Commons
import qs.Ui
import "Model.js" as Model
import "settings"

// Spitball's settings overlay: a centered card over a scrim, the same
// surface Omarchy uses for its menu, emoji, and clipboard pickers, per
// docs/SPEC-v2.md section 1. A full-screen transparent layer-shell window
// on the Overlay layer with exclusive keyboard focus while open; the card
// itself (settings/SettingsCard.qml) does the nav, pages, and key handling.
//
// Opened by Widget.qml's openSettings(section) (`show(section)` here, with
// `open` bound to the widget's settingsOpened flag). Dismissed by Esc, a
// click on the scrim, or the card's ✕ -- all of which route through
// requestClose() so the owner flips the one bool.
//
// The layering rule from the old dropdown carries over unchanged (see
// settings/SettingsStore.qml): the store asks for requestClose() before it
// launches a portal picker, an installer, or a pkexec prompt, and
// requestReopen() once a synchronous picker resolves.
PanelWindow {
  id: root

  property bool open: false
  required property string cliPath
  required property string runtimeDir
  property color foreground: Color.popups.text
  property string fontFamily: Style.font.family
  // The daemon's parsed state.json (Widget.qml's `st`), for the header
  // line, the nav's "Set up" badge, and the About page.
  property var st: Model.emptyState()

  signal requestClose()
  signal requestReopen()

  readonly property alias store: settingsStore
  readonly property string section: card.section

  // Card geometry: roomy enough for two columns of controls and full
  // paths, capped to the screen minus the theme's outer gap.
  readonly property int cardWidth: Math.min(Style.space(680), root.width - Style.gapsOut * 2)
  readonly property int cardHeight: Math.min(Style.space(480), root.height - Style.gapsOut * 2)

  // Switches the card to `sectionId` (unknown ids read as General). Safe
  // whether or not the overlay is open yet -- the owner calls this, then
  // flips `open`.
  function show(sectionId) {
    card.showSection(sectionId)
  }

  visible: open || cardSurface.opacity > 0
  color: "transparent"
  exclusionMode: ExclusionMode.Ignore

  WlrLayershell.namespace: "spitball-settings"
  WlrLayershell.layer: WlrLayer.Overlay
  // Exclusive while open (the emoji/menu pattern: a modal surface on one
  // output), released the moment the logical close fires so the fade-out
  // never locks the keyboard.
  WlrLayershell.keyboardFocus: open ? WlrKeyboardFocus.Exclusive : WlrKeyboardFocus.None

  anchors {
    top: true
    bottom: true
    left: true
    right: true
  }

  onOpenChanged: {
    if (open) Qt.callLater(function() { if (root.open) card.focusKeys() })
  }

  SettingsStore {
    id: settingsStore
    cliPath: root.cliPath
    runtimeDir: root.runtimeDir
    active: root.open
    foreground: root.foreground
    fontFamily: root.fontFamily
    st: root.st
    onRequestClose: root.requestClose()
    onRequestReopen: root.requestReopen()
  }

  Rectangle {
    id: scrim
    anchors.fill: parent
    color: Color.menu.scrim
    opacity: root.open ? 1.0 : 0
    Behavior on opacity { NumberAnimation { duration: 140; easing.type: Easing.OutCubic } }
  }

  // Outside click: anywhere on the scrim closes. Disabled during the
  // fade-out so a dying overlay doesn't swallow a click meant for the app
  // behind it.
  MouseArea {
    anchors.fill: parent
    enabled: root.open
    acceptedButtons: Qt.AllButtons
    onClicked: root.requestClose()
  }

  BorderSurface {
    id: cardSurface
    anchors.centerIn: parent
    width: root.cardWidth
    height: root.cardHeight
    radius: Style.cornerRadius
    color: Color.popups.background
    borderSpec: Border.surfaceSpec("popups", "border", Color.popups.border, Math.max(1, Style.space(2)))
    padding: Style.spacing.panelPadding
    opacity: root.open ? 1.0 : 0
    Behavior on opacity { NumberAnimation { duration: 140; easing.type: Easing.OutCubic } }

    // Swallow clicks on the card so they don't reach the scrim's dismiss area.
    MouseArea {
      anchors.fill: parent
      acceptedButtons: Qt.AllButtons
    }

    SettingsCard {
      id: card
      anchors.fill: parent
      anchors.topMargin: cardSurface.contentTopInset
      anchors.rightMargin: cardSurface.contentRightInset
      anchors.bottomMargin: cardSurface.contentBottomInset
      anchors.leftMargin: cardSurface.contentLeftInset
      store: settingsStore
      onCloseRequested: root.requestClose()
    }
  }
}
