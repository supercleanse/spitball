.pragma library

// Pure helpers for Spitball widget. No QML types in here, so state
// parsing and formatting stay easy to read apart from the layout. Mirrors
// programs/Spitball/CONTRACT.md -- this is the only file that needs to
// change if the state-file shape ever does.

var KNOWN_STATES = ["offline", "idle", "detected", "recording", "processing", "error"]

function emptyState() {
  return {
    state: "offline", app: "", started_at: 0, auto_record: false,
    message: "", last_call: null, updated_at: 0
  }
}

// Parses the daemon's state.json text. Per the contract: a missing or
// unparseable file is treated as {"state": "offline"}. Always returns a
// fully-populated object (never null) so callers can bind straight to its
// fields without null-checking every read.
function parseState(text) {
  var base = emptyState()
  try {
    var s = JSON.parse(String(text || ""))
    if (!s || typeof s !== "object") return base
    for (var k in base) if (s[k] === undefined) s[k] = base[k]
    if (KNOWN_STATES.indexOf(s.state) === -1) s.state = "offline"
    if (s.last_call !== null && typeof s.last_call !== "object") s.last_call = null
    return s
  } catch (e) {
    return base
  }
}

function pad2(n) {
  return n < 10 ? "0" + n : String(n)
}

// "m:ss" under an hour, "h:mm:ss" at or past one hour, per the contract.
function elapsedLabel(startedAt, nowMs) {
  var started = Number(startedAt) || 0
  if (started <= 0) return "0:00"
  var secs = Math.max(0, Math.floor(nowMs / 1000 - started))
  var h = Math.floor(secs / 3600)
  var m = Math.floor((secs % 3600) / 60)
  var s = secs % 60
  return h > 0 ? (h + ":" + pad2(m) + ":" + pad2(s)) : (m + ":" + pad2(s))
}

function tooltipFor(st) {
  switch (st.state) {
    case "detected": return "Call detected in " + (st.app || "an app") + ": click to record"
    case "recording": return "Recording " + (st.app || "call") + ": click to stop"
    case "processing": return st.message || "Processing…"
    case "error": return st.message || "Spitball error"
    case "idle": return "No call in progress: click to record"
    default: return "Spitball: daemon not running"
  }
}

// Clips text for a popup menu row so a long call title can't blow out the
// panel width -- the shared Button component doesn't elide its own label.
function truncate(text, max) {
  var t = String(text || "")
  return t.length > max ? t.slice(0, max - 1) + "…" : t
}

function lastCallLabel(lastCall) {
  if (!lastCall || !lastCall.title) return "Open last summary"
  return "Open last summary: " + truncate(lastCall.title, 34)
}
