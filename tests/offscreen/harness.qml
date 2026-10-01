import QtQuick
import Quickshell
import qs.Commons
import qs.Ui
import "Model.js" as Model
import "settings"

// Offscreen render harness for the settings overlay -- run ONLY through
// tests/offscreen/render.sh (QT_QPA_PLATFORM=offscreen, a fake CLI). It
// renders settings/SettingsCard.qml inside a plain FloatingWindow dressed
// like SettingsWindow.qml's card (scrim color behind, the same
// BorderSurface, padding, and size), cycles through every section, and
// grabs one PNG per section into $SPITBALL_SHOT_DIR. The real layer-shell
// PanelWindow chrome can't map without a Wayland compositor, so this is the
// card, not the window.
//
// Nothing here ships: the fake CLI path and the fake daemon state come from
// environment variables render.sh sets, and the real plugin never loads
// this file.
ShellRoot {
  id: shell

  readonly property string shotDir: Quickshell.env("SPITBALL_SHOT_DIR") || "/tmp"
  readonly property string fakeCli: Quickshell.env("SPITBALL_FAKE_CLI") || ""
  readonly property string fakeRuntime: Quickshell.env("SPITBALL_FAKE_RUNTIME") || "/tmp"
  readonly property var sections: Model.settingsSections()
  property int shotIndex: 0

  readonly property int cardWidth: Style.space(680)
  readonly property int cardHeight: Style.space(480)

  FloatingWindow {
    id: win
    visible: true
    implicitWidth: shell.cardWidth + Style.space(40)
    implicitHeight: shell.cardHeight + Style.space(40)
    color: Color.background

    SettingsStore {
      id: store
      cliPath: shell.fakeCli
      runtimeDir: shell.fakeRuntime
      active: true
      st: Model.parseState(JSON.stringify({
        state: "idle", app: "", auto_record: true,
        setup_needed: "Add a Deepgram key",
        last_call: { title: "Weekly sync", dir: "/home/demo/Calls/x" }
      }))
    }

    // Sized explicitly rather than filling the window: the offscreen
    // platform may leave the window at its default size, and grabToImage
    // renders the item at its own size regardless.
    Item {
      id: stage
      width: shell.cardWidth + Style.space(40)
      height: shell.cardHeight + Style.space(40)

      Rectangle {
        anchors.fill: parent
        color: Color.menu.scrim
      }

      BorderSurface {
        id: cardSurface
        anchors.centerIn: parent
        width: shell.cardWidth
        height: shell.cardHeight
        radius: Style.cornerRadius
        color: Color.popups.background
        borderSpec: Border.surfaceSpec("popups", "border", Color.popups.border, Math.max(1, Style.space(2)))
        padding: Style.spacing.panelPadding

        SettingsCard {
          id: card
          anchors.fill: parent
          anchors.topMargin: cardSurface.contentTopInset
          anchors.rightMargin: cardSurface.contentRightInset
          anchors.bottomMargin: cardSurface.contentBottomInset
          anchors.leftMargin: cardSurface.contentLeftInset
          store: store
        }
      }
    }

    // First shot after the fake CLI's round trips have landed, then one
    // section every 350 ms (enough for the page Loader and layout).
    Timer {
      id: cycle
      interval: 900
      running: true
      repeat: true
      onTriggered: {
        if (shell.shotIndex >= shell.sections.length) {
          cycle.stop()
          Qt.quit()
          return
        }
        var entry = shell.sections[shell.shotIndex]
        card.showSection(entry.id)
        cycle.interval = 350
        grab.section = entry.id
        grab.restart()
      }
    }

    Timer {
      id: grab
      property string section: ""
      interval: 250
      onTriggered: {
        var target = shell.shotDir + "/settings-" + grab.section + ".png"
        stage.grabToImage(function(result) {
          if (!result.saveToFile(target)) console.warn("harness: failed to save " + target)
          else console.info("harness: wrote " + target)
          shell.shotIndex += 1
        })
      }
    }
  }
}
