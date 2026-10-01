"""spitball.speakers tests: folding far-side ids into labels, the speakers
map, the calendar 1:1 shortcut, the LLM naming pass (the model is always a
fake -- no network), rendering with confidence, and the CLI helpers."""
import json
import unittest
from unittest import mock

from spitball import config, speakers
from spitball.providers import deepgram
from tests.testutil import load_fixture


def _u(channel, start, end, text, speaker=0, **extra):
    return dict({"channel": channel, "speaker": speaker, "start": start, "end": end, "transcript": text}, **extra)


def _cfg(**over):
    cfg = dict(config.DEFAULTS, my_name="Morgan")
    cfg.update(over)
    return cfg


MEETING = {"title": "Weekly sync", "attendees": [
    {"name": "Priya Nair", "email": "priya@example.com", "response": "accepted", "self": False},
    {"name": "Alex Demo", "email": "alex@example.com", "response": "needs_action", "self": False},
    {"name": "Sam Declined", "email": "sam@example.com", "response": "declined", "self": False},
    {"name": "", "email": "me@example.com", "response": "accepted", "self": True},
]}


class TestFarSpeakerOrder(unittest.TestCase):
    def test_ordered_by_first_appearance(self):
        utts = [_u(1, 10, 20, "later voice " * 5, speaker=7), _u(1, 0, 8, "first voice " * 5, speaker=3),
                _u(0, 30, 31, "me")]
        order, fold = speakers.far_speaker_order(utts)
        self.assertEqual(order, [3, 7])
        self.assertEqual(fold, {})

    def test_tiny_voice_folds_into_the_one_before_it(self):
        utts = [_u(1, 0, 20, "a long stretch of talk from the first person " * 3, speaker=0),
                _u(1, 20.5, 21.5, "mm-hm", speaker=4),
                _u(1, 22, 40, "and the second person goes on for a while too " * 3, speaker=1)]
        order, fold = speakers.far_speaker_order(utts)
        self.assertEqual(order, [0, 1])
        self.assertEqual(fold, {4: 0})

    def test_tiny_voice_at_the_start_folds_into_the_one_after(self):
        utts = [_u(1, 0, 1, "hi", speaker=9),
                _u(1, 2, 30, "the real first speaker talking at length " * 3, speaker=2)]
        order, fold = speakers.far_speaker_order(utts)
        self.assertEqual(order, [2])
        self.assertEqual(fold, {9: 2})

    def test_everyone_tiny_keeps_everyone(self):
        # Two people who each said one sentence: no bigger voice to fold into.
        utts = [_u(1, 0, 1, "ship it friday", speaker=2), _u(1, 5, 6, "no wait", speaker=5)]
        order, fold = speakers.far_speaker_order(utts)
        self.assertEqual(order, [2, 5])
        self.assertEqual(fold, {})

    def test_max_speakers_folds_the_quietest_extra_voice(self):
        utts = [_u(1, 0, 30, "one " * 40, speaker=0), _u(1, 31, 60, "two " * 40, speaker=1),
                _u(1, 61, 70, "three " * 20, speaker=2), _u(1, 71, 100, "four " * 40, speaker=3)]
        order, fold = speakers.far_speaker_order(utts, max_speakers=3)
        self.assertEqual(order, [0, 1, 3])
        self.assertEqual(fold, {2: 1})

    def test_max_one_means_one_voice(self):
        utts = [_u(1, 0, 30, "one " * 40, speaker=0), _u(1, 31, 60, "two " * 40, speaker=1)]
        order, fold = speakers.far_speaker_order(utts, max_speakers=1)
        self.assertEqual(order, [0])
        self.assertEqual(fold, {1: 0})

    def test_failed_and_blank_utterances_do_not_create_voices(self):
        utts = [_u(1, 0, 30, "one " * 40, speaker=0), _u(1, 31, 40, "", speaker=1),
                _u(1, 41, 50, "[transcription failed for this part]", speaker=2, failed=True)]
        order, fold = speakers.far_speaker_order(utts)
        self.assertEqual(order, [0])

    def test_no_far_speech(self):
        self.assertEqual(speakers.far_speaker_order([_u(0, 0, 1, "me")]), ([], {}))


class TestMapAndLabels(unittest.TestCase):
    def test_normalize_map_makes_an_entry_per_label(self):
        m = speakers.normalize_map(None, [3, 7])
        self.assertEqual(sorted(m), ["1", "2"])
        self.assertEqual(m["1"], speakers.empty_entry(3))
        self.assertEqual(m["2"]["id"], 7)

    def test_normalize_map_keeps_cached_entries_by_provider_id_and_drops_gone_ones(self):
        raw = {"1": {"id": 7, "name": "Priya Nair", "confidence": "high", "source": "user", "evidence": "set by hand"},
               "2": {"id": 99, "name": "Gone", "confidence": "high", "source": "llm", "evidence": "x"}}
        m = speakers.normalize_map(raw, [3, 7])
        self.assertEqual(m["1"]["name"], "")            # id 3 has no cached entry
        self.assertEqual(m["2"]["name"], "Priya Nair")  # id 7's entry moved to label 2
        self.assertEqual(m["2"]["source"], "user")

    def test_normalize_map_scrubs_garbage(self):
        raw = {"1": {"id": 3, "name": "  X ", "confidence": "sure", "source": "psychic"},
               "2": {"id": 7, "name": "", "confidence": "high", "source": "llm", "evidence": "stale"}}
        m = speakers.normalize_map(raw, [3, 7])
        self.assertEqual(m["1"], {"id": 3, "name": "X", "confidence": "none", "source": "", "evidence": ""})
        self.assertEqual(m["2"]["confidence"], "none")  # no name means nothing else survives
        self.assertEqual(m["2"]["evidence"], "")

    def test_label_rendering_by_confidence(self):
        e = lambda **k: dict(speakers.empty_entry(0), **k)
        self.assertEqual(speakers.label(2, e(), False), "Speaker 2")
        self.assertEqual(speakers.label(1, e(), True), "Them")
        self.assertEqual(speakers.label(2, e(name="Priya Nair", confidence="high", source="llm"), False), "Priya Nair")
        self.assertEqual(speakers.label(2, e(name="Priya Nair", confidence="medium", source="llm"), False),
                         "Speaker 2 (probably Priya Nair)")
        self.assertEqual(speakers.label(1, e(name="Priya Nair", confidence="medium", source="llm"), True),
                         "Them (probably Priya Nair)")
        self.assertEqual(speakers.label(2, e(name="Priya Nair", confidence="low", source="llm"), False), "Speaker 2")
        self.assertEqual(speakers.label(2, e(name="Priya Nair", confidence="none", source="user"), False), "Priya Nair")
        self.assertEqual(speakers.label(2, e(name="Priya Nair", confidence="high", source="llm"), False, named=False),
                         "Speaker 2")

    def test_summary_lines(self):
        m = {"1": dict(speakers.empty_entry(0), name="Priya Nair", confidence="high", source="llm"),
             "2": dict(speakers.empty_entry(1), name="Alex Demo", confidence="medium", source="llm"),
             "3": speakers.empty_entry(2)}
        self.assertEqual(speakers.summary_lines(m, single=False),
                         ["**Speakers:** Speaker 1 = Priya Nair, Speaker 2 (probably Alex Demo), Speaker 3"])
        self.assertEqual(speakers.summary_lines({"1": speakers.empty_entry(0)}, single=True), [])
        self.assertEqual(speakers.summary_lines({"1": speakers.empty_entry(0), "2": speakers.empty_entry(1)}, single=False), [])
        self.assertEqual(speakers.summary_lines({"1": m["1"]}, single=True), ["**Speakers:** Them = Priya Nair"])
        self.assertEqual(speakers.summary_lines({}, single=False), [])

    def test_mismatch_line(self):
        self.assertEqual(speakers.mismatch_line(3, 3), "")
        self.assertEqual(speakers.mismatch_line(None, 4), "")
        self.assertEqual(speakers.mismatch_line(3, 0), "")
        self.assertIn("lists 3 other people; 5 voices were found", speakers.mismatch_line(3, 5))
        self.assertIn("lists 1 other person; 2 voices were found", speakers.mismatch_line(1, 2))
        self.assertIn("1 voice was found", speakers.mismatch_line(3, 1))


class TestCandidates(unittest.TestCase):
    def test_far_invitees_only(self):
        self.assertEqual([c["name"] for c in speakers.candidates(MEETING)], ["Priya Nair", "Alex Demo"])

    def test_nameless_invitee_is_counted_never_a_candidate_and_dedup(self):
        # Codex review 3: a speaker name must never be an address (it would
        # be rendered into the transcript the summary model reads).
        m = {"attendees": [{"name": "", "email": "x@example.com", "response": "accepted", "self": False},
                           {"name": "X", "email": "x2@example.com", "response": "accepted", "self": False},
                           {"name": "x", "email": "x3@example.com", "response": "accepted", "self": False},
                           {"name": "y@example.com", "email": "y@example.com", "response": "accepted", "self": False},
                           {"name": "", "email": "", "response": "accepted", "self": False}]}
        self.assertEqual([c["name"] for c in speakers.candidates(m)], ["X"])
        self.assertEqual(speakers.nameless_count(m), 2)  # x@ and y@ (the empty one is nobody)
        self.assertEqual(speakers.nameless_count(None), 0)
        self.assertEqual(speakers.nameless_count(MEETING), 0)

    def test_no_meeting(self):
        self.assertEqual(speakers.candidates(None), [])
        self.assertEqual(speakers.candidates({"attendees": "nope"}), [])

    def test_match_candidate_exact_and_first_name_never_address(self):
        cands = speakers.candidates(MEETING)
        self.assertEqual(speakers._match_candidate("priya nair", cands), "Priya Nair")
        self.assertEqual(speakers._match_candidate("Priya", cands), "Priya Nair")
        self.assertEqual(speakers._match_candidate("alex", cands), "Alex Demo")
        # The model is never shown an address, so one it writes is a guess, not a match.
        self.assertEqual(speakers._match_candidate("alex@example.com", cands), "")
        self.assertEqual(speakers._match_candidate("Sam Declined", cands), "")  # not a candidate
        self.assertEqual(speakers._match_candidate("Nobody", cands), "")
        self.assertEqual(speakers._match_candidate("", cands), "")

    def test_nameless_invitee_can_never_be_named_by_the_model(self):
        # Codex P1 then review 3: an invitee with no name on the invite used
        # to reach the model as their address, then as a placeholder that
        # mapped back to the address as a speaker name. Neither now.
        m = {"attendees": [{"name": "Priya Nair", "email": "priya@example.com", "response": "accepted", "self": False},
                           {"name": "", "email": "x@example.com", "response": "accepted", "self": False}]}
        cands = speakers.candidates(m)
        self.assertEqual([c["name"] for c in cands], ["Priya Nair"])
        self.assertEqual(speakers.nameless_count(m), 1)
        for guess in ("Invitee 2", "x@example.com", "x"):
            self.assertEqual(speakers._match_candidate(guess, cands), "", guess)
        self.assertEqual(speakers.parse_answers(
            json.dumps({"Speaker 1": {"name": "x@example.com", "confidence": "high", "evidence": "0:01 intro"}}),
            cands), {})

    def test_ambiguous_first_name_is_no_match(self):
        m = {"attendees": [{"name": "Sam One", "email": "", "response": "accepted", "self": False},
                           {"name": "Sam Two", "email": "", "response": "accepted", "self": False}]}
        self.assertEqual(speakers._match_candidate("Sam", speakers.candidates(m)), "")


class TestParseAnswers(unittest.TestCase):
    def setUp(self):
        self.cands = speakers.candidates(MEETING)

    def test_strict_json_with_evidence(self):
        reply = json.dumps({"Speaker 1": {"name": "Priya Nair", "confidence": "high", "evidence": "0:42 'thanks Priya'"},
                            "Speaker 2": None})
        got = speakers.parse_answers(reply, self.cands)
        self.assertEqual(got, {"1": {"name": "Priya Nair", "confidence": "high", "evidence": "0:42 'thanks Priya'"}})

    def test_json_inside_prose_and_think_blocks(self):
        reply = "<think>hmm</think>Sure! Here you go:\n```json\n{\"2\": {\"name\": \"alex\", \"confidence\": \"medium\", \"evidence\": \"intro\"}}\n```"
        got = speakers.parse_answers(reply, self.cands)
        self.assertEqual(got["2"]["name"], "Alex Demo")
        self.assertEqual(got["2"]["confidence"], "medium")

    def test_name_not_on_invite_is_dropped(self):
        reply = json.dumps({"Speaker 1": {"name": "Sam Declined", "confidence": "high", "evidence": "x"},
                            "Speaker 2": {"name": "Zed", "confidence": "high", "evidence": "x"}})
        self.assertEqual(speakers.parse_answers(reply, self.cands), {})

    def test_high_without_evidence_becomes_medium(self):
        reply = json.dumps({"Speaker 1": {"name": "Priya Nair", "confidence": "high", "evidence": ""}})
        self.assertEqual(speakers.parse_answers(reply, self.cands)["1"]["confidence"], "medium")

    def test_unknown_confidence_is_low_and_low_is_dropped(self):
        reply = json.dumps({"Speaker 1": {"name": "Priya Nair", "confidence": "certain", "evidence": "x"},
                            "Speaker 2": {"name": "Alex Demo", "confidence": "low", "evidence": "x"}})
        self.assertEqual(speakers.parse_answers(reply, self.cands), {})

    def test_same_name_for_two_speakers_makes_both_uncertain(self):
        reply = json.dumps({"Speaker 1": {"name": "Priya Nair", "confidence": "high", "evidence": "x"},
                            "Speaker 2": {"name": "Priya", "confidence": "high", "evidence": "y"}})
        self.assertEqual(speakers.parse_answers(reply, self.cands), {})

    def test_no_json_raises(self):
        with self.assertRaises(RuntimeError):
            speakers.parse_answers("I can't tell.", self.cands)
        with self.assertRaises(RuntimeError):
            speakers.parse_answers("{not json", self.cands)
        with self.assertRaises(RuntimeError):
            speakers.parse_answers("[1, 2]", self.cands)


class TestResolve(unittest.TestCase):
    """resolve(): the pipeline's one call. The model is a fake `chat`."""

    def _normalized(self, far=2, meeting=MEETING, speakers_block=None):
        utts = [_u(0, 0, 3, "hi everyone this is Morgan")]
        for i in range(far):
            utts.append(_u(1, 5 + 10 * i, 12 + 10 * i, f"voice {i} says a good number of words here ok", speaker=i))
        n = {"provider": "deepgram", "model": "nova-3", "utterances": utts}
        if meeting:
            n["meeting"] = meeting
        if speakers_block is not None:
            n["speakers"] = speakers_block
        return n

    def test_one_to_one_names_them_without_the_model(self):
        meeting = {"attendees": [MEETING["attendees"][0], MEETING["attendees"][3]]}
        n = self._normalized(far=1, meeting=meeting)
        chat = mock.Mock(side_effect=AssertionError("must not be called"))
        rep = speakers.resolve(n, _cfg(), "transcript", chat=chat)
        self.assertEqual(rep["method"], "calendar")
        self.assertEqual(n["speakers"]["1"]["name"], "Priya Nair")
        self.assertEqual(n["speakers"]["1"]["source"], "calendar")
        self.assertEqual(n["speakers"]["1"]["confidence"], "high")

    def test_one_invitee_but_two_voices_goes_to_the_model(self):
        meeting = {"attendees": [MEETING["attendees"][0], MEETING["attendees"][3]]}
        n = self._normalized(far=2, meeting=meeting)
        chat = mock.Mock(return_value=json.dumps({"Speaker 2": {"name": "Priya Nair", "confidence": "high", "evidence": "e"}}))
        rep = speakers.resolve(n, _cfg(), "transcript", chat=chat)
        self.assertEqual(rep["method"], "llm")
        self.assertEqual(n["speakers"]["1"]["name"], "")
        self.assertEqual(n["speakers"]["2"]["name"], "Priya Nair")

    def test_llm_pass_prompt_carries_invite_and_transcript(self):
        n = self._normalized()
        chat = mock.Mock(return_value=json.dumps({"Speaker 1": {"name": "Alex Demo", "confidence": "medium", "evidence": "0:05 intro"}}))
        rep = speakers.resolve(n, _cfg(), "**[00:00:05] Speaker 1:** hello", chat=chat)
        self.assertEqual(rep["method"], "llm")
        system, user, cfg, max_tokens = chat.call_args[0]
        self.assertIn("Morgan", system)
        self.assertIn("- Priya Nair\n", user)
        self.assertIn("- Alex Demo\n", user)
        self.assertNotIn("@", user)  # names only: no address ever leaves the machine
        self.assertNotIn("Sam Declined", user)
        self.assertIn("**[00:00:05] Speaker 1:** hello", user)
        self.assertEqual(n["speakers"]["1"], {"id": 0, "name": "Alex Demo", "confidence": "medium",
                                              "source": "llm", "evidence": "0:05 intro"})
        self.assertEqual(n["speakers"]["2"]["name"], "")

    def test_llm_prompt_counts_a_nameless_invitee_and_never_names_them(self):
        meeting = {"attendees": [MEETING["attendees"][0],
                                 {"name": "", "email": "x@example.com", "response": "accepted", "self": False}]}
        n = self._normalized(meeting=meeting)
        chat = mock.Mock(return_value=json.dumps({"Speaker 2": {"name": "x@example.com", "confidence": "high", "evidence": "0:15 'this is me'"}}))
        speakers.resolve(n, _cfg(), "t", chat=chat)
        user = chat.call_args[0][1]
        self.assertIn("- Priya Nair\n", user)
        self.assertIn("(and 1 more invitee with no name on the invite)", user)
        self.assertNotIn("@", user)
        self.assertEqual(n["speakers"]["2"]["name"], "")  # an address is never a speaker name

    def test_one_named_invitee_plus_a_nameless_one_is_not_a_one_to_one(self):
        meeting = {"attendees": [MEETING["attendees"][0],
                                 {"name": "", "email": "x@example.com", "response": "accepted", "self": False}]}
        n = self._normalized(far=1, meeting=meeting)
        chat = mock.Mock(return_value="{}")
        rep = speakers.resolve(n, _cfg(), "t", chat=chat)
        self.assertEqual(rep["method"], "llm")  # two people were invited: the model has to say which one
        chat.assert_called_once()

    def test_naming_is_governed_by_speaker_names_alone(self):
        # The Calendar page's calendar_names_to_summary is about the summary
        # request; naming can't work without the names, so it still runs.
        n = self._normalized()
        chat = mock.Mock(return_value="{}")
        rep = speakers.resolve(n, _cfg(calendar_names_to_summary=False), "t", chat=chat)
        self.assertEqual(rep["method"], "llm")
        chat.assert_called_once()

    def test_user_entries_are_never_recomputed(self):
        block = {"1": {"id": 0, "name": "Hand Named", "confidence": "high", "source": "user", "evidence": "set by hand"},
                 "2": {"id": 1, "name": "Old Guess", "confidence": "high", "source": "llm", "evidence": "old"}}
        n = self._normalized(speakers_block=block)
        chat = mock.Mock(return_value=json.dumps({"Speaker 1": {"name": "Priya Nair", "confidence": "high", "evidence": "e"},
                                                  "Speaker 2": None}))
        speakers.resolve(n, _cfg(), "t", chat=chat)
        self.assertEqual(n["speakers"]["1"]["name"], "Hand Named")   # user wins over the model
        self.assertEqual(n["speakers"]["2"]["name"], "")             # the stale automatic guess was cleared

    def test_all_user_entries_skips_the_model(self):
        block = {"1": {"id": 0, "name": "A", "confidence": "high", "source": "user", "evidence": "x"},
                 "2": {"id": 1, "name": "B", "confidence": "high", "source": "user", "evidence": "x"}}
        n = self._normalized(speakers_block=block)
        chat = mock.Mock(side_effect=AssertionError("must not be called"))
        self.assertEqual(speakers.resolve(n, _cfg(), "t", chat=chat)["method"], "user")

    def test_naming_off_or_no_meeting_leaves_entries_empty(self):
        for cfg, n in ((_cfg(speaker_names=False), self._normalized()), (_cfg(), self._normalized(meeting=None))):
            chat = mock.Mock(side_effect=AssertionError("must not be called"))
            rep = speakers.resolve(n, cfg, "t", chat=chat)
            self.assertEqual(rep["method"], "none")
            self.assertEqual(rep["error"], "")
            self.assertEqual([e["name"] for e in n["speakers"].values()], ["", ""])

    def test_summaries_off_means_no_model_call(self):
        n = self._normalized()
        chat = mock.Mock(side_effect=AssertionError("must not be called"))
        rep = speakers.resolve(n, _cfg(summary_enabled=False), "t", chat=chat)
        self.assertIn("summary_enabled", rep["error"])

    def test_model_failure_is_reported_not_raised(self):
        n = self._normalized()
        chat = mock.Mock(side_effect=RuntimeError("summary model unreachable at http://x"))
        rep = speakers.resolve(n, _cfg(), "t", chat=chat)
        self.assertEqual(rep["method"], "none")
        self.assertIn("unreachable", rep["error"])
        self.assertEqual(n["speakers"]["1"]["name"], "")

    def test_no_far_speech_removes_the_block(self):
        n = {"utterances": [_u(0, 0, 1, "just me")], "meeting": MEETING, "speakers": {"1": {"id": 0, "name": "x"}}}
        rep = speakers.resolve(n, _cfg(), "t", chat=mock.Mock())
        self.assertNotIn("speakers", n)
        self.assertEqual(rep["speakers"], {})

    def test_long_transcript_is_truncated_for_the_model(self):
        n = self._normalized()
        chat = mock.Mock(return_value="{}")
        speakers.resolve(n, _cfg(), "x" * (speakers.NAMING_MAX_CHARS + 500), chat=chat)
        user = chat.call_args[0][1]
        self.assertIn("[transcript truncated]", user)
        self.assertLess(len(user), speakers.NAMING_MAX_CHARS + 400)

    def test_real_chat_helper_goes_through_process_chat_completion(self):
        with mock.patch("spitball.process.chat_completion", return_value="{}") as cc:
            self.assertEqual(speakers._chat("s", "u", _cfg(), 800), "{}")
        cc.assert_called_once_with("s", "u", mock.ANY, max_tokens=800, temperature=0.0)


class TestCliHelpers(unittest.TestCase):
    def _normalized(self):
        return {"utterances": [
            _u(1, 0, 10, "first voice with plenty of words to count here " * 2, speaker=2),
            _u(1, 12, 20, "second voice also with plenty of words to count " * 2, speaker=5),
            _u(0, 22, 24, "me")], "speakers": {}}

    def test_set_name_and_clear(self):
        n = self._normalized()
        m = speakers.set_name(n, _cfg(), 2, "  Priya Nair ")
        self.assertEqual(m["2"], {"id": 5, "name": "Priya Nair", "confidence": "high", "source": "user",
                                  "evidence": "set by hand"})
        self.assertIs(n["speakers"], m)
        m = speakers.set_name(n, _cfg(), 2, "")
        self.assertEqual(m["2"]["name"], "")
        self.assertEqual(m["2"]["source"], "")

    def test_set_name_unknown_number(self):
        with self.assertRaises(ValueError):
            speakers.set_name(self._normalized(), _cfg(), 3, "X")

    def test_listing_and_format(self):
        n = self._normalized()
        speakers.set_name(n, _cfg(), 1, "Alex Demo")
        rows = speakers.listing(n, _cfg())
        self.assertEqual([r["n"] for r in rows], [1, 2])
        self.assertEqual(rows[0]["label"], "Alex Demo")
        self.assertEqual(rows[0]["id"], 2)
        self.assertEqual(rows[0]["words"], 18)
        self.assertEqual(rows[0]["seconds"], 10.0)
        self.assertEqual(rows[1]["label"], "Speaker 2")
        text = speakers.format_listing(rows)
        self.assertIn("1  Speaker 1  Alex Demo (user, high)", text)
        self.assertIn("2  Speaker 2  (unnamed)", text)
        self.assertEqual(speakers.format_listing([]), "No far-side speech in this call.")

    def test_format_listing_single_voice_says_them(self):
        n = {"utterances": [_u(1, 0, 10, "one voice " * 5, speaker=0)]}
        self.assertIn("1  Them       (unnamed)", speakers.format_listing(speakers.listing(n, _cfg())))

    def test_rename_in_summary(self):
        body = ("# Sync\n\n- Speaker 2 will ship it. Speaker 1 (probably Alex Demo) agreed;\n"
                "- Speaker 12 is untouched, Speaker 2's plan stands (Speaker 2)")
        old = {"1": "Speaker 1 (probably Alex Demo)", "2": "Speaker 2", "12": "Speaker 12"}
        new = {"1": "Alex Demo", "2": "Priya Nair", "12": "Speaker 12"}
        out = speakers.rename_in_summary(body, old, new)
        self.assertIn("- Priya Nair will ship it. Alex Demo agreed;", out)
        self.assertIn("Speaker 12 is untouched, Priya Nair's plan stands (Priya Nair)", out)

    def test_rename_in_summary_them_and_old_name(self):
        body = "Them asked twice. Later, Wrong Name asked again."
        out = speakers.rename_in_summary(body, {"1": "Wrong Name"}, {"1": "Right Name"})
        self.assertEqual(out, "Right Name asked twice. Later, Right Name asked again.")


class TestCarryUserNames(unittest.TestCase):
    """carry_user_names(): Codex P2 regression -- `reprocess --retranscribe`
    used to overwrite the cache with a provider result that has no
    `speakers` block, and every hand-set name went with it."""

    def _fresh(self, ids, provider="deepgram", split=None):
        n = {"provider": provider, "model": "m",
             "utterances": [_u(1, 5 + 10 * i, 12 + 10 * i, "a good long stretch of words spoken here", speaker=s)
                            for i, s in enumerate(ids)]}
        if split is not None:
            n["diarization"] = {"ran": split, "engine": "sherpa-onnx", "expected": None, "found": len(ids)}
        return n

    def _old(self, ids=(2, 5, 7), **kw):
        n = self._fresh(list(ids), **kw)
        n["speakers"] = {
            "1": {"id": 2, "name": "Priya N.", "confidence": "high", "source": "user", "evidence": "set by hand"},
            "2": {"id": 5, "name": "Old Guess", "confidence": "high", "source": "llm", "evidence": "e"},
            "3": {"id": 7, "name": "Sam Hand", "confidence": "high", "source": "user", "evidence": "set by hand"}}
        return n

    def test_same_ids_carry_over_and_automatic_entries_do_not(self):
        n = self._fresh([2, 5, 7])
        self.assertEqual(speakers.carry_user_names(self._old(), n, _cfg()), [])
        block = speakers.normalize_map(n["speakers"], [2, 5, 7])
        self.assertEqual((block["1"]["name"], block["1"]["source"]), ("Priya N.", "user"))
        self.assertEqual(block["2"]["name"], "")  # the llm guess is resolve()'s job again
        self.assertEqual((block["3"]["name"], block["3"]["source"]), ("Sam Hand", "user"))
        self.assertNotIn("speakers_dropped", n)

    def test_a_gone_id_is_dropped_and_recorded(self):
        # Same provider and split, same voice count (3): ids are comparable; one is gone.
        n = self._fresh([2, 9, 5])
        dropped = speakers.carry_user_names(self._old(), n, _cfg())
        self.assertEqual(dropped, [{"name": "Sam Hand", "id": 7, "was": "Speaker 3", "reason": "that voice is gone"}])
        self.assertEqual(n["speakers_dropped"], dropped)
        block = speakers.normalize_map(n["speakers"], [2, 9, 5])
        self.assertEqual(block["1"]["name"], "Priya N.")
        self.assertEqual(block["2"]["name"], "")

    def test_single_voice_reads_them(self):
        old = self._fresh([0])
        old["speakers"] = {"1": {"id": 0, "name": "Only One", "confidence": "high", "source": "user", "evidence": "x"}}
        n = self._fresh([3])
        self.assertEqual(speakers.carry_user_names(old, n, _cfg()),
                         [{"name": "Only One", "id": 0, "was": "Them", "reason": "that voice is gone"}])

    # ---- Codex review 3: ids only mean the same thing inside the same frame

    def test_identity_of_a_transcript(self):
        self.assertEqual(speakers.identity(self._fresh([2, 5])), {"provider": "deepgram", "split": "deepgram", "voices": 2})
        self.assertEqual(speakers.identity(self._fresh([0], provider="local")), {"provider": "local", "split": "none", "voices": 1})
        self.assertEqual(speakers.identity(self._fresh([0, 1], provider="local", split=True)),
                         {"provider": "local", "split": "sherpa-onnx", "voices": 2})
        self.assertEqual(speakers.identity(self._fresh([0], provider="local", split=False)),
                         {"provider": "local", "split": "none", "voices": 1})
        self.assertEqual(speakers.identity(None), {"provider": "", "split": "none", "voices": 0})
        self.assertEqual(speakers.identity_mismatch(speakers.identity(self._fresh([2, 5])), speakers.identity(self._fresh([5, 2]))), "")

    def test_provider_change_drops_every_hand_set_name_with_the_reason(self):
        # Deepgram speaker 0 named by hand; re-transcribed with the local
        # provider unsplit, whose speaker 0 is everyone on the far side.
        old = self._fresh([0, 1])
        old["speakers"] = {"1": {"id": 0, "name": "Alice", "confidence": "high", "source": "user", "evidence": "x"},
                           "2": {"id": 1, "name": "", "confidence": "none", "source": "", "evidence": ""}}
        n = self._fresh([0], provider="local")
        dropped = speakers.carry_user_names(old, n, _cfg())
        self.assertEqual(dropped, [{"name": "Alice", "id": 0, "was": "Speaker 1", "reason": "provider changed (deepgram → local)"}])
        self.assertNotIn("speakers", n)  # nothing carried: "Them" stays "Them"
        self.assertIn("(provider changed (deepgram → local))", speakers.dropped_line(dropped))
        # A split that came or went, or a different voice count, is the same refusal.
        old2 = self._fresh([0, 1], provider="local", split=True)
        old2["speakers"] = {"1": {"id": 0, "name": "Alice", "confidence": "high", "source": "user", "evidence": "x"}}
        d2 = speakers.carry_user_names(old2, self._fresh([0], provider="local"), _cfg())
        self.assertEqual(d2[0]["reason"], "speaker split changed (sherpa-onnx → none)")
        d3 = speakers.carry_user_names(old2, self._fresh([0, 1, 2], provider="local", split=True), _cfg())
        self.assertEqual(d3[0]["reason"], "a different number of far-side voices (2 → 3)")
        # Same frame: carried as before.
        n4 = self._fresh([1, 0], provider="local", split=True)
        self.assertEqual(speakers.carry_user_names(old2, n4, _cfg()), [])
        self.assertEqual(speakers.normalize_map(n4["speakers"], [1, 0])["2"]["name"], "Alice")

    def test_nothing_to_carry(self):
        n = self._fresh([2])
        self.assertEqual(speakers.carry_user_names(None, n, _cfg()), [])
        self.assertEqual(speakers.carry_user_names({"speakers": "junk"}, n, _cfg()), [])
        self.assertEqual(speakers.carry_user_names({"speakers": {"1": {"id": "?", "name": "X", "source": "user"}}}, n, _cfg()), [])
        self.assertNotIn("speakers", n)
        # A stale dropped record from an earlier run is cleared by the next carry.
        n["speakers_dropped"] = [{"name": "Stale", "id": 1, "was": "Speaker 1"}]
        speakers.carry_user_names(None, n, _cfg())
        self.assertNotIn("speakers_dropped", n)

    def test_dropped_line_wording(self):
        self.assertEqual(speakers.dropped_line(None), "")
        self.assertEqual(speakers.dropped_line([]), "")
        self.assertEqual(speakers.dropped_line([{"name": ""}]), "")
        one = speakers.dropped_line([{"name": "Sam Hand", "id": 7, "was": "Speaker 3"}])
        self.assertTrue(one.startswith("**Note:** re-transcribing changed the far-side voices, so 1 hand-set name "
                                       "could not be carried over: Sam Hand (was Speaker 3). Set it again"), one)
        two = speakers.dropped_line([{"name": "A", "was": "Speaker 1"}, {"name": "B", "was": "Speaker 2"}])
        self.assertIn("2 hand-set names could not be carried over: A (was Speaker 1), B (was Speaker 2). Set them again", two)

    def test_setting_a_dropped_name_again_clears_its_record(self):
        n = self._fresh([2, 9, 5])
        speakers.carry_user_names(self._old(), n, _cfg())
        speakers.set_name(n, _cfg(), 2, "sam hand")  # case-insensitive
        self.assertNotIn("speakers_dropped", n)
        self.assertEqual(n["speakers"]["2"]["name"], "sam hand")
        # A different name leaves the record (and its note) in place.
        n2 = self._fresh([2, 9, 5])
        speakers.carry_user_names(self._old(), n2, _cfg())
        speakers.set_name(n2, _cfg(), 2, "Someone Else")
        self.assertEqual(n2["speakers_dropped"][0]["name"], "Sam Hand")


class TestDeepgramFixture(unittest.TestCase):
    def test_multi_speaker_fixture_keeps_both_voices(self):
        dg = deepgram.normalize(load_fixture("deepgram_multi_speaker.json"), _cfg())
        order, fold = speakers.far_speaker_order(dg["utterances"], 6)
        self.assertEqual(order, [2, 5])
        _, _, labels = speakers.labels_for(dg, _cfg())
        self.assertEqual(labels, {2: "Speaker 1", 5: "Speaker 2"})


if __name__ == "__main__":
    unittest.main()
