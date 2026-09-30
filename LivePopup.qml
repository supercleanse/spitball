pragma ComponentBehavior: Bound
import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

// The Live popup's content -- meant to be embedded inside a KeyboardPanel's
// content item (see Widget.qml), same pattern as SettingsPanel.qml. Shows a
// chat-style live transcript fed by $XDG_RUNTIME_DIR/spitball/live.json
// while a call is recording, per docs/SPEC-live-transcript.md. This file has
// no transcription logic of its own -- it only reads live.json (via a
// FileView, watched, never polled) and shells out `spitball stop` for its
// one button. `st` is the SAME parsed state.json object Widget.qml already
// maintains (Model.parseState's shape) -- passed in rather than re-read here
// so the popup and the bar dot can never disagree about whether a call is
// still recording.
Item {
  id: root

  property color foreground: Color.popups.text
  property color accent: Color.urgent
  property string fontFamily: Style.font.family
  // Absolute path to bin/spitball, exactly as Widget.qml resolves it.
  required property string cliPath
  // $XDG_RUNTIME_DIR/spitball -- watched for live.json.
  required property string runtimeDir
  // Bind to the owning KeyboardPanel's `open`. Only watches live.json while
  // true, same convention as SettingsPanel's model.json watch.
  property bool active: false
  // The daemon's own parsed state (Model.parseState shape) -- Widget.qml's
  // `st`. Drives the header (app/elapsed) and the "recording just stopped"
  // overlay; this file never parses state.json itself.
  required property var st

  signal requestClose()

  readonly property string liveStatePath: root.runtimeDir + "/live.json"
  property var liveState: Model.emptyLiveState()
  readonly property bool isRecording: root.st.state === "recording"

  FileView {
    path: root.liveStatePath
    watchChanges: root.active
    printErrors: false
    onFileChanged: reload()
    onLoaded: root.liveState = Model.parseLiveState(text())
    onLoadFailed: function(error) { root.liveState = Model.emptyLiveState() }
  }

  // Fire-and-forget CLI call, same shape as Widget.qml's own runCli -- the
  // only command this popup ever sends is `stop`.
  function runCli(args) {
    Quickshell.execDetached(["/usr/bin/python3", "-I", root.cliPath].concat(args))
  }

  // ------------------------------------------------------------ elapsed clock
  property double nowMs: Date.now()
  readonly property string elapsedText: Model.elapsedLabel(root.st.started_at, root.nowMs)
  Timer {
    interval: 1000
    running: root.active && root.isRecording
    repeat: true
    onTriggered: root.nowMs = Date.now()
  }
  onIsRecordingChanged: if (isRecording) root.nowMs = Date.now()

  // ------------------------------------------------------------ auto-close
  // Per spec: once the recording stops, show a brief "saved, transcribing"
  // message and auto-close after a few seconds (the user can also just
  // click Close, or Esc/outside-click like any other popup).
  Timer {
    id: autoCloseTimer
    interval: 6000
    running: root.active && !root.isRecording
    onTriggered: root.requestClose()
  }

  // ================================================================ UI

  Column {
    id: layout
    anchors.fill: parent
    spacing: Style.space(10)

    // ---- header ----
    Item {
      id: headerItem
      width: parent.width
      height: Math.max(dotText.implicitHeight, stopButton.implicitHeight, headerCloseButton.implicitHeight)

      Row {
        id: headerLeft
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
        spacing: Style.space(6)

        Text {
          id: dotText
          textFormat: Text.PlainText
          text: "●"
          color: root.isRecording ? root.accent : Qt.darker(root.foreground, 1.6)
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          anchors.verticalCenter: parent.verticalCenter
        }

        Text {
          textFormat: Text.PlainText
          text: (root.st.app || "Spitball") + (root.isRecording ? " — " + root.elapsedText : "")
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          font.bold: true
          anchors.verticalCenter: parent.verticalCenter
        }
      }

      // The only way to stop from this popup -- one click, no confirm.
      Button {
        id: stopButton
        anchors.right: headerCloseButton.left
        anchors.rightMargin: Style.space(6)
        anchors.verticalCenter: parent.verticalCenter
        visible: root.isRecording
        text: "Stop recording"
        bordered: true
        foreground: root.foreground
        fontFamily: root.fontFamily
        fontSize: Style.font.caption
        onClicked: root.runCli(["stop"])
      }

      // Closes the popup only -- the recording keeps going.
      Button {
        id: headerCloseButton
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        text: "✕"
        tooltipText: "Close (recording continues)"
        foreground: root.foreground
        fontFamily: root.fontFamily
        fontSize: Style.font.caption
        onClicked: root.requestClose()
      }
    }

    PanelSeparator { id: sepTop; width: parent.width; foreground: root.foreground }

    // ---- transcript body ----
    // Takes whatever's left after the header, both separators, the status
    // row, and the four inter-item gaps a 5-child Column puts between them --
    // the Column itself is height-fixed (anchors.fill above), so it never
    // stretches children on its own; this is the one that's meant to flex.
    Item {
      id: bodyArea
      width: parent.width
      height: layout.height - headerItem.height - sepTop.height - sepBottom.height
        - statusRow.implicitHeight - layout.spacing * 4

      Flickable {
        id: flick
        anchors.fill: parent
        contentWidth: width
        contentHeight: bubbleColumn.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        interactive: contentHeight > height

        property real lastContentHeight: 0
        onContentHeightChanged: {
          var wasAtBottom = flick.lastContentHeight <= 0
            || Model.liveShouldAutoScroll(flick.contentY, flick.height, flick.lastContentHeight)
          flick.lastContentHeight = contentHeight
          if (wasAtBottom) {
            newMessagesPill.visible = false
            Qt.callLater(function() {
              flick.contentY = Math.max(0, flick.contentHeight - flick.height)
            })
          } else {
            newMessagesPill.visible = true
          }
        }

        Column {
          id: bubbleColumn
          width: flick.width
          spacing: Style.space(10)
          topPadding: Style.space(2)
          bottomPadding: Style.space(2)

          Text {
            textFormat: Text.PlainText
            visible: bubbleRepeater.count === 0
            width: parent.width
            text: root.isRecording ? "Waiting for speech…" : "No transcript for this call."
            color: Qt.darker(root.foreground, 1.5)
            wrapMode: Text.WordWrap
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          Repeater {
            id: bubbleRepeater
            model: Model.liveBubbles(root.liveState.utterances)

            delegate: Column {
              id: bubbleItem
              required property var modelData
              width: bubbleColumn.width
              spacing: Style.space(2)

              readonly property bool mine: modelData.channel === 0
              readonly property string side: Model.liveBubbleSide(modelData.channel)

              Text {
                textFormat: Text.PlainText
                visible: bubbleItem.modelData.showLabel
                anchors.right: bubbleItem.mine ? parent.right : undefined
                anchors.left: bubbleItem.mine ? undefined : parent.left
                // state.json carries no my_name field (see CONTRACT.md) --
                // liveSpeakerLabel() falls back to a plain "Me" for channel 0.
                text: Model.liveSpeakerLabel(bubbleItem.modelData.channel, "")
                color: Qt.darker(root.foreground, 1.4)
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
              }

              Item {
                width: parent.width
                height: bubble.implicitHeight

                Rectangle {
                  id: bubble
                  anchors.right: bubbleItem.mine ? parent.right : undefined
                  anchors.left: bubbleItem.mine ? undefined : parent.left
                  width: Math.min(bubbleColumn.width * 0.8, bubbleText.implicitWidth + Style.space(20))
                  implicitHeight: bubbleText.implicitHeight + Style.space(14)
                  radius: Style.cornerRadius
                  color: bubbleItem.mine
                    ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.18)
                    : Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.08)

                  Column {
                    anchors.fill: parent
                    anchors.margins: Style.space(7)
                    spacing: Style.space(2)

                    Text {
                      id: bubbleText
                      textFormat: Text.PlainText
                      width: Math.min(bubbleColumn.width * 0.8 - Style.space(14), implicitWidth)
                      text: bubbleItem.modelData.failed
                        ? "(couldn't transcribe this part)" : bubbleItem.modelData.transcript
                      color: bubbleItem.modelData.failed ? Qt.darker(root.foreground, 1.5) : root.foreground
                      font.italic: bubbleItem.modelData.failed
                      // A partial is the line still being spoken -- dimmed
                      // until the pause locks it in.
                      opacity: bubbleItem.modelData.partial ? 0.65 : 1
                      wrapMode: Text.WordWrap
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.body
                    }

                    Text {
                      textFormat: Text.PlainText
                      text: Model.liveTimestampLabel(bubbleItem.modelData.start)
                      color: Qt.darker(root.foreground, 1.6)
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.caption
                    }
                  }
                }
              }
            }
          }
        }
      }

      // "New messages ↓" pill -- shown only while the user has scrolled up
      // and new content has landed below the fold (per spec).
      Button {
        id: newMessagesPill
        visible: false
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottom: parent.bottom
        anchors.bottomMargin: Style.space(8)
        text: "New messages ↓"
        bordered: true
        selected: true
        foreground: root.foreground
        fontFamily: root.fontFamily
        fontSize: Style.font.caption
        onClicked: {
          newMessagesPill.visible = false
          flick.contentY = Math.max(0, flick.contentHeight - flick.height)
        }
      }
    }

    PanelSeparator { id: sepBottom; width: parent.width; foreground: root.foreground }

    // ---- status line / post-stop overlay ----
    Item {
      id: statusRow
      width: parent.width
      implicitHeight: statusText.implicitHeight

      Text {
        id: statusText
        textFormat: Text.PlainText
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        text: root.isRecording ? Model.liveStatusText(root.liveState) : "Recording saved; transcribing…"
        color: Qt.darker(root.foreground, 1.3)
        wrapMode: Text.WordWrap
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
      }

    }
  }
}
