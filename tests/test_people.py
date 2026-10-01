"""spitball.people tests: a name for an invitee the calendar lists by
address only (the book, the invite, the meeting title, the address), and
the 1:1 and hand-rename paths in spitball.speakers that use it."""
import json
import unittest
from unittest import mock

from spitball import config, people, speakers


def _a(email, name="", response="accepted", self_=False):
    return {"name": name, "email": email, "response": response, "self": self_}


ME = _a("morgan@example.com", "Morgan Lee", self_=True)


class BookCase(unittest.TestCase):
    def setUp(self):
        self.addCleanup(lambda: people._book_path().unlink(missing_ok=True))
        people._book_path().unlink(missing_ok=True)


class TestFromTitle(BookCase):
    def test_title_word_the_address_starts_with(self):
        self.assertEqual(people.from_title("robert@example.com", "Q3 Review: Rob", []), "Rob")
        self.assertEqual(people.from_title("jordan@example.com", "Q3 Review: Jordan", []), "Jordan")

    def test_lowercase_and_stop_words_never_name_anyone(self):
        self.assertEqual(people.from_title("andrew@example.com", "Marketing and planning", []), "")
        self.assertEqual(people.from_title("reviewer@example.com", "Review", []), "")

    def test_a_word_two_invitees_share_names_neither(self):
        peers = ["chris@example.com", "christine@example.com"]
        self.assertEqual(people.from_title("chris@example.com", "Chris sync", peers), "")

    def test_role_mailbox_is_never_named_from_the_title(self):
        self.assertEqual(people.from_title("support@example.com", "Support handoff", []), "")


class TestFromAddress(BookCase):
    def test_first_dot_last_is_sure(self):
        self.assertEqual(people.from_address("priya.nair@example.com"), ("Priya Nair", True))
        self.assertEqual(people.from_address("mary-jo_smith@example.com"), ("Mary Jo Smith", True))

    def test_one_part_first_name_is_unsure(self):
        self.assertEqual(people.from_address("jordan@example.com"), ("Jordan", False))
        self.assertEqual(people.from_address("robert+cal@example.com"), ("Robert", False))

    def test_initial_plus_surname_digits_and_roles_give_nothing(self):
        for email in ("jsmith@example.com", "bwilliams@example.com", "x1@example.com",
                      "info@example.com", "no-reply@example.com", "jo@example.com"):
            self.assertEqual(people.from_address(email), ("", False), email)


class TestDisplayName(BookCase):
    def test_ladder_book_then_invite_then_title_then_address(self):
        a = _a("robert@example.com")
        self.assertEqual(people.display_name(a, "Weekly sync"), ("Robert", "address", False))
        self.assertEqual(people.display_name(a, "Q3 Review: Rob"), ("Rob", "title", True))
        named = _a("robert@example.com", "Robert Example")
        self.assertEqual(people.display_name(named, "Q3 Review: Rob"), ("Robert Example", "invite", True))
        people.remember("Robert@Example.com", "Rob")
        self.assertEqual(people.display_name(named, "Weekly sync"), ("Rob", "book", True))

    def test_a_name_that_is_an_address_falls_through_to_the_address(self):
        a = _a("priya.nair@example.com", "priya.nair@example.com")
        self.assertEqual(people.display_name(a), ("Priya Nair", "address", True))

    def test_the_book_never_stores_an_address_as_a_name(self):
        self.assertFalse(people.remember("x@example.com", "y@example.com"))
        self.assertFalse(people.remember("not-an-address", "Name"))
        self.assertEqual(people.load_book(), {})
        self.assertTrue(people.remember("x@example.com", "Xavier"))
        self.assertFalse(people.remember("x@example.com", "Xavier"))  # unchanged
        self.assertEqual(json.loads(people._book_path().read_text()), {"x@example.com": "Xavier"})


def _normalized(meeting, far=1):
    utts = [{"channel": 0, "speaker": 0, "start": 0, "end": 4, "transcript": "hi there how are you doing"}]
    for i in range(far):
        utts.append({"channel": 1, "speaker": i, "start": 5 + i * 20, "end": 20 + i * 20,
                     "transcript": "a good long stretch of talk from this person " * 3})
    return {"utterances": utts, "meeting": meeting}


def _cfg():
    return dict(config.DEFAULTS, my_name="Morgan")


class TestOneToOneWithAnAddressOnlyInvitee(BookCase):
    def test_title_names_the_far_side_without_the_model(self):
        n = _normalized({"title": "Q3 Review: Rob", "attendees": [_a("robert@example.com"), ME]})
        chat = mock.Mock(side_effect=AssertionError("must not be called"))
        rep = speakers.resolve(n, _cfg(), "transcript", chat=chat)
        self.assertEqual(rep["method"], "calendar")
        e = n["speakers"]["1"]
        self.assertEqual((e["name"], e["confidence"], e["source"]), ("Rob", "high", "calendar"))
        self.assertIn("meeting title", e["evidence"])
        self.assertEqual(speakers.labels_for(n, _cfg())[2], {0: "Rob"})

    def test_bare_one_part_address_reads_as_probably(self):
        n = _normalized({"title": "Weekly sync", "attendees": [_a("jordan@example.com"), ME]})
        speakers.resolve(n, _cfg(), "transcript", chat=mock.Mock(side_effect=AssertionError))
        self.assertEqual(n["speakers"]["1"]["confidence"], "medium")
        self.assertEqual(speakers.labels_for(n, _cfg())[2], {0: "Them (probably Jordan)"})

    def test_hand_rename_on_a_one_to_one_is_remembered_for_the_next_call(self):
        meeting = {"title": "Weekly sync", "attendees": [_a("jordan@example.com"), ME]}
        n = _normalized(meeting)
        speakers.resolve(n, _cfg(), "transcript", chat=mock.Mock(side_effect=AssertionError))
        before = dict(n["speakers"]["1"])
        speakers.set_name(n, _cfg(), 1, "Jordan Reyes")
        self.assertEqual(speakers.learn_name(n, _cfg(), 1, "Jordan Reyes", before), "jordan@example.com")
        later = _normalized(meeting)
        speakers.resolve(later, _cfg(), "transcript", chat=mock.Mock(side_effect=AssertionError))
        self.assertEqual(speakers.labels_for(later, _cfg())[2], {0: "Jordan Reyes"})

    def test_hand_rename_of_an_unnamed_voice_in_a_group_learns_nothing(self):
        meeting = {"title": "Weekly sync", "attendees": [_a("priya.nair@example.com"), _a("x1@example.com"), ME]}
        n = _normalized(meeting, far=2)
        self.assertEqual(speakers.learn_name(n, _cfg(), 2, "Sam", {"name": "", "source": ""}), "")
        self.assertEqual(people.load_book(), {})

    def test_address_only_invitees_reach_the_naming_model_as_names(self):
        meeting = {"title": "Weekly sync", "attendees": [_a("priya.nair@example.com"), _a("x1@example.com"), ME]}
        n = _normalized(meeting, far=2)
        chat = mock.Mock(return_value=json.dumps({"Speaker 1": {"name": "Priya Nair", "confidence": "high", "evidence": "0:05 thanks Priya"}}))
        speakers.resolve(n, _cfg(), "transcript", chat=chat)
        user = chat.call_args[0][1]
        self.assertIn("- Priya Nair", user)
        self.assertNotIn("@", user)
        self.assertIn("1 more invitee with no name", user)
        self.assertEqual(n["speakers"]["1"]["name"], "Priya Nair")


class TestInviteLists(BookCase):
    def test_summary_list_names_address_only_invitees_and_header_keeps_the_address(self):
        from spitball import calendar as cal
        ev = {"title": "Q3 Review: Rob", "organizer": {"name": "Morgan Lee", "email": "morgan@example.com"},
              "attendees": [_a("robert@example.com"), _a("x1@example.com"), ME]}
        self.assertEqual(cal.invite_names(ev), ["Rob", "and 1 more with no name on the invite"])
        self.assertEqual(cal.attendee_names(ev), ["Rob <robert@example.com>", "x1@example.com"])


if __name__ == "__main__":
    unittest.main()
