"""A display name for an invitee the calendar lists by address only.

Google (and most workspace calendars) often put a colleague on the invite
as a bare address with no name. Without a name that person can never be a
speaker candidate, so a plain 1:1 rendered as "Them". This module finds a
name for them, in order of authority:

  book     the name you gave this address before: `spitball speakers <dir>
           <n> "Rob"` on a call where that speaker is clearly this
           invitee records address -> name in people.json, and every later
           call uses it.
  invite   the name on the invite itself (CN) -- the old and still
           preferred source when present.
  title    a capitalized word in the meeting title that the address starts
           with: "Q3 Review: Rob" + robert@ -> "Rob". Only one far
           invitee may match the word, or none of them gets it.
  address  the address's own parts: priya.nair@ -> "Priya Nair" (sure);
           jordan@ -> "Jordan" (unsure, so a 1:1 reads "Them (probably
           Jordan)" until you confirm it once). Role mailboxes (info@,
           support@) and initial-plus-surname shapes (jsmith@) give nothing.

The result is always a name, never an address, so the CONTRACT rule that
no model payload carries an email address still holds: the address only
ever leaves this module as the key in the local book.
"""
from __future__ import annotations

import json
import re

from . import config

BOOK_NAME = "people.json"

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Mailboxes that are a function, not a person.
_ROLES = {
    "admin", "accounts", "accounting", "billing", "bookings", "calendar", "contact",
    "dev", "events", "finance", "hello", "help", "hi", "hr", "info", "invoices",
    "jobs", "mail", "marketing", "media", "meet", "meetings", "no", "noreply",
    "notifications", "office", "ops", "payroll", "press", "reply", "sales",
    "schedule", "scheduling", "security", "service", "support", "team", "test",
    "zoom",
}

# Title words that are never anybody's name, even capitalized.
_TITLE_STOP = {
    "and", "the", "for", "with", "sync", "call", "meeting", "review", "weekly",
    "monthly", "daily", "team", "chat", "intro", "catch", "check", "plan",
    "planning", "standup", "quarterly", "update", "demo", "kickoff", "interview",
}

# Two-letter word starts English names actually use. A single-part address
# opening with anything else ("js" in jsmith, "bw" in bwilliams) is an
# initial plus a surname, not a first name.
_ONSETS = {
    "bl", "br", "ch", "cl", "cr", "dr", "dw", "fl", "fr", "gl", "gr", "gw", "kh",
    "kl", "kn", "kr", "ph", "pl", "pr", "rh", "sc", "sh", "sk", "sl", "sm", "sn",
    "sp", "st", "sv", "sw", "th", "tr", "ts", "tw", "vl", "wh", "wr", "zh",
}
_VOWELS = set("aeiouy")


def _book_path():
    return config.STATE_DIR / BOOK_NAME


def load_book() -> dict:
    """{address (lowercased): name}. Missing or unreadable is empty."""
    try:
        data = json.loads(_book_path().read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k).strip().lower(): str(v).strip() for k, v in data.items()
            if isinstance(k, str) and isinstance(v, str) and v.strip() and "@" in k
            and not _EMAIL_RE.search(v)}


def remember(email: str, name: str) -> bool:
    """Records address -> name in the book. A name that is itself an
    address is never stored. Returns True when the book changed."""
    email = (email or "").strip().lower()
    name = (name or "").strip()
    if "@" not in email or not name or _EMAIL_RE.search(name):
        return False
    book = load_book()
    if book.get(email) == name:
        return False
    book[email] = name
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    config.atomic_write(_book_path(), json.dumps(book, indent=1, sort_keys=True), mode=0o600)
    return True


def _local_parts(email: str) -> list:
    local = (email or "").split("@", 1)[0].split("+", 1)[0].lower()
    return [p for p in re.split(r"[._\-]+", local) if p]


def _first(email: str) -> str:
    parts = _local_parts(email)
    return parts[0] if parts and parts[0].isalpha() else ""


def _title_words(title: str) -> list:
    return [w for w in re.findall(r"[A-Za-z][A-Za-z'’]*", title or "")
            if len(w) >= 3 and w[0].isupper() and w.lower() not in _TITLE_STOP]


def from_title(email: str, title: str, peers: list) -> str:
    """The title word this address starts with, if no other far invitee's
    address starts with it too. `peers` are the other far invitees' addresses."""
    first = _first(email)
    if len(first) < 3 or first in _ROLES:
        return ""
    for word in _title_words(title):
        w = word.lower()
        if not first.startswith(w):
            continue
        if any(_first(p).startswith(w) for p in peers if p and p.lower() != email.lower()):
            return ""
        return word[0].upper() + word[1:]
    return ""


def from_address(email: str) -> tuple:
    """(name, sure) from the address alone; ("", False) when it doesn't
    look like a person's name."""
    parts = _local_parts(email)
    if not parts or any(not p.isalpha() for p in parts) or any(p in _ROLES for p in parts):
        return "", False
    if len(parts) >= 2:
        if len(parts) > 3 or any(len(p) < 2 for p in parts):
            return "", False
        return " ".join(p.capitalize() for p in parts), True
    p = parts[0]
    if len(p) < 3 or len(p) > 12 or not (set(p) & _VOWELS):
        return "", False
    if p[0] not in _VOWELS and p[1] not in _VOWELS and p[:2] not in _ONSETS:
        return "", False
    return p.capitalize(), False


def display_name(attendee: dict, title: str = "", peers: list | None = None,
                 book: dict | None = None) -> tuple:
    """(name, source, sure) for one invitee; ("", "", False) when there is
    no name to be had. `source` is "book" | "invite" | "title" | "address".
    `peers`: every far invitee's address (used to keep a title word from
    naming two people). `book`: load_book(), passed in to read it once."""
    if not isinstance(attendee, dict):
        return "", "", False
    email = (attendee.get("email") or "").strip()
    name = (attendee.get("name") or "").strip()
    if book is None:
        book = load_book()
    if email and book.get(email.lower()):
        return book[email.lower()], "book", True
    if name and not _EMAIL_RE.search(name):
        return name, "invite", True
    if not email:
        m = _EMAIL_RE.search(name)
        email = m.group(0) if m else ""
    if not email:
        return "", "", False
    t = from_title(email, title, peers or [])
    if t:
        return t, "title", True
    a, sure = from_address(email)
    if a:
        return a, "address", sure
    return "", "", False
