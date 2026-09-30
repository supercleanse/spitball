import QtQuick
import qs.Commons
import qs.Ui

// Calendar: placeholder for the ICS feed + calendar_command hook
// (docs/SPEC-v2.md section 2). Until then, what the feature will do.
SettingsPage {
  id: page
  title: "Calendar"

  ComingSoon {
    store: page.store
    text: "Match each call to a calendar event from a secret iCal/ICS address (or your own command), then use it to name the call folder, head the transcript with the meeting and its attendees, and feed the summarizer."
  }

  NoteText {
    store: page.store
    text: "No Google sign-in and no shipped client ID: the feed address is stored as a secret, like an API key, and calendar failures never block a recording."
  }
}
