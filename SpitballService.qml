import QtQuick
import Quickshell
import Quickshell.Io

// Keeps the Spitball daemon running for as long as the shell is up: one daemon
// per session, shared by every bar instance. The daemon owns detection,
// recording and processing; the widget only reads its state file and runs CLI
// commands (see CONTRACT.md). If the daemon exits, restart it with backoff.
Item {
  id: root

  property int restartCount: 0
  property bool destroying: false

  // Absolute paths keep launch behavior independent of PATH and Python hooks.
  readonly property string cliPath: Qt.resolvedUrl("bin/spitball").toString().replace(/^file:\/\//, "")

  Process {
    id: daemon
    command: ["/usr/bin/python3", "-I", root.cliPath, "daemon"]
    // Inherit the session environment: pactl needs XDG_RUNTIME_DIR, notify-send
    // needs the D-Bus session address.
    running: true
    onExited: function(code, status) {
      if (root.destroying) return
      root.restartCount += 1
      restartTimer.interval = Math.min(60000, 1000 * Math.pow(2, Math.min(6, root.restartCount)))
      restartTimer.restart()
    }
  }

  Timer {
    id: restartTimer
    repeat: false
    onTriggered: if (!root.destroying && !daemon.running) daemon.running = true
  }

  // A daemon that has stayed up for a while resets the backoff.
  Timer {
    interval: 120000
    repeat: true
    running: daemon.running
    onTriggered: root.restartCount = 0
  }

  // Stopping the process sends SIGTERM; the daemon finishes the current
  // recording file and picks up its processing on the next start.
  Component.onDestruction: {
    root.destroying = true
    restartTimer.stop()
    if (daemon.running) daemon.running = false
  }

  IpcHandler {
    target: "supercleanse.spitball"

    function restart(): string {
      root.restartCount = 0
      if (daemon.running) daemon.running = false
      restartTimer.interval = 500
      restartTimer.restart()
      return "ok"
    }
  }
}
