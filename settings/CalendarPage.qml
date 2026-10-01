import QtQuick
import qs.Commons
import qs.Ui
import "../Model.js" as Model

// Calendar (docs/SPEC-v2.md section 2): on/off, the source (a secret
// iCal/ICS address or the user's own command), the secret or the command
// itself, a Test button that shows what a call starting now would match,
// what a match is used for, and the rarely-needed keys under Advanced.
// Every round trip goes through the store (`calendar test --json`,
// `config set`/`set-secret`); this file only lays out rows.
SettingsPage {
  id: page
  title: "Calendar"

  readonly property bool calendarOn: !!page.store.value("calendar_enabled", false)
  readonly property string source: Model.calendarSourceChoice(page.store.value("calendar_source", "ics"))

  ToggleRow {
    store: page.store
    label: "Match calls to calendar events"
    checked: page.calendarOn
    disabledLook: page.store.loadFailed
    onToggled: page.store.setKey("calendar_enabled", !checked)
  }
  NoteText {
    store: page.store
    text: "A confident match names the call folder, heads the transcript with the meeting and its attendees, and tells the summarizer who was invited. A weak match changes nothing, and calendar trouble never blocks a recording."
  }
  ErrorNote { store: page.store; key: "calendar_enabled" }

  Dropdown {
    width: Style.space(260)
    label: "Source"
    options: Model.calendarSourceOptions()
    value: page.source
    foreground: page.store.foreground
    fontFamily: page.store.fontFamily
    onChanged: function(v) { page.store.setKey("calendar_source", v) }
  }
  ErrorNote { store: page.store; key: "calendar_source" }

  // ---- built-in source: the secret address ----
  Column {
    visible: page.source === "ics"
    width: parent.width
    spacing: Style.space(10)

    SecretRow {
      store: page.store
      label: "Secret iCal address"
      placeholder: Model.keySourcePlaceholder(page.store.secretInfo("calendar_ics_url"))
      disabledLook: page.store.loadFailed
      onSaved: function(t) { page.store.setSecret("calendar_ics_url", t) }
    }
    NoteText {
      store: page.store
      text: "Google Calendar: Settings → your calendar → Integrate calendar → \"Secret address in iCal format\". Any ICS or webcal address works (Outlook, Fastmail, Nextcloud…). It is a credential: anyone holding it can read the calendar until you reset it, so it is stored like an API key and never shown again."
    }
    ErrorNote { store: page.store; key: "calendar_ics_url" }
  }

  // ---- escape hatch: a command printing JSON events ----
  Column {
    visible: page.source === "command"
    width: parent.width
    spacing: Style.space(10)

    FieldRow {
      store: page.store
      label: "Command"
      text: page.store.value("calendar_command", "")
      placeholder: "e.g. my-calendar-events"
      disabledLook: page.store.loadFailed
      onCommitted: function(t) { page.store.setKey("calendar_command", t) }
    }
    NoteText {
      store: page.store
      text: "A shell command whose output is a JSON array of events (title, start, end, attendees…; the shape is in CONTRACT.md). It gets the time window as SPITBALL_WINDOW_START / SPITBALL_WINDOW_END. Use it to plug in khal, gcalcli, a CalDAV script, or anything else."
    }
    ErrorNote { store: page.store; key: "calendar_command" }
  }

  TestRow {
    store: page.store
    busy: page.store.calendarTesting
    message: page.store.calendarTestMsg
    disabledLook: page.store.loadFailed
    onRun: page.store.testCalendar()
  }
  NoteText { store: page.store; text: "Shows which event a call starting right now would match. `spitball calendar test --at 14:30` checks another time." }
  Text {
    visible: page.store.calendarOwnerUnknown
    width: parent.width
    textFormat: Text.PlainText
    text: "Couldn't tell which address in this feed is yours (no address clearly dominates, and a tie is never guessed). Set \"Your calendar email\" under Advanced so your own replies are read and speaker naming knows which side is you."
    color: Color.urgent
    wrapMode: Text.WordWrap
    font.family: page.store.fontFamily
    font.pixelSize: Style.font.caption
  }

  PanelSeparator { foreground: page.store.foreground }

  // ---- what a match is used for ----
  ToggleRow {
    store: page.store
    label: "Use the event title as the call title"
    checked: !!page.store.value("calendar_prefer_event_title", true)
    disabledLook: page.store.loadFailed
    onToggled: page.store.setKey("calendar_prefer_event_title", !checked)
  }
  NoteText { store: page.store; text: "On: the folder, transcript, and summary take the event's title. Off: the summarizer's own title stays, with the meeting header added." }
  ErrorNote { store: page.store; key: "calendar_prefer_event_title" }

  ToggleRow {
    store: page.store
    label: "Send attendee names to the summarizer"
    checked: !!page.store.value("calendar_names_to_summary", true)
    disabledLook: page.store.loadFailed
    onToggled: page.store.setKey("calendar_names_to_summary", !checked)
  }
  NoteText { store: page.store; text: "On: the summary request lists who was on the invite (names only, never addresses), wherever the Summary endpoint points (a local model by default). Off: the summarizer gets the meeting's title and time and nothing about the people. transcript.md and summary.md keep their own Attendees line either way, and speaker naming (Speakers page) has its own switch." }
  ErrorNote { store: page.store; key: "calendar_names_to_summary" }

  ToggleRow {
    store: page.store
    label: "Send the event description too"
    checked: !!page.store.value("calendar_description_to_summary", false)
    disabledLook: page.store.loadFailed
    onToggled: page.store.setKey("calendar_description_to_summary", !checked)
  }
  NoteText { store: page.store; text: "On: the event's description goes into the summary request as well. Off by default: descriptions can carry private text and add little." }
  ErrorNote { store: page.store; key: "calendar_description_to_summary" }

  PanelSeparator { foreground: page.store.foreground }

  // ---- meeting reminders (spitball/reminders.py) ----
  ToggleRow {
    store: page.store
    label: "Remind me before meetings with a video link"
    checked: !!page.store.value("calendar_reminders", true)
    disabledLook: page.store.loadFailed || !page.calendarOn
    onToggled: page.store.setKey("calendar_reminders", !checked)
  }
  NoteText {
    store: page.store
    text: "Just before an event with a Zoom, Google Meet, Teams, or Webex link starts, a Spitball notification offers Join & record: it opens the link and starts recording, pinned to that event, and stops when the call ends. Dismiss does nothing. Only https links on those services are ever opened. Nothing fires while a recording is already running, and `spitball calendar upcoming` lists what is due."
  }
  ErrorNote { store: page.store; key: "calendar_reminders" }

  NumberRow {
    store: page.store
    label: "Remind this long before the start"
    value: Number(page.store.value("calendar_remind_before_s", 60))
    from: 0; to: 900; stepSize: 15; unit: "s"
    disabledLook: page.store.loadFailed || !page.calendarOn
    onCommitted: function(v) { page.store.setKey("calendar_remind_before_s", v) }
  }
  ErrorNote { store: page.store; key: "calendar_remind_before_s" }

  RevealRow {
    store: page.store

    FieldRow {
      store: page.store
      visible: page.source === "ics"
      label: "Address command"
      text: page.store.value("calendar_ics_url_command", "")
      placeholder: "e.g. op read op://Private/Calendar/url"
      disabledLook: page.store.loadFailed
      onCommitted: function(t) { page.store.setKey("calendar_ics_url_command", t) }
    }
    NoteText { store: page.store; visible: page.source === "ics"; text: "A shell command whose output is the secret address, for a password manager." }
    ErrorNote { store: page.store; key: "calendar_ics_url_command" }

    FieldRow {
      store: page.store
      label: "Your calendar email"
      text: page.store.value("calendar_my_email", "")
      placeholder: "Empty: detected from the feed"
      disabledLook: page.store.loadFailed
      onCommitted: function(t) { page.store.setKey("calendar_my_email", t) }
    }
    NoteText { store: page.store; text: "So your own response is read from invites (declined ones are skipped). The address on nearly every invite in the feed is taken as yours when this is empty." }
    ErrorNote { store: page.store; key: "calendar_my_email" }

    NumberRow {
      store: page.store
      visible: page.source === "ics"
      label: "Re-download the feed after"
      value: Number(page.store.value("calendar_cache_ttl_s", 900))
      from: 60; to: 86400; stepSize: 60; unit: "s"
      disabledLook: page.store.loadFailed
      onCommitted: function(v) { page.store.setKey("calendar_cache_ttl_s", v) }
    }
    ErrorNote { store: page.store; key: "calendar_cache_ttl_s" }
  }
}
