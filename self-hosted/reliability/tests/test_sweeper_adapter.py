"""Порты sweeper (go-live): parse_open_prs + has_completed_review через store."""
import unittest

from reliability.sweeper import OpenPR
from reliability.sweeper_adapter import make_has_completed_review, parse_open_prs
from reliability.state import Event, State, StateStore


class TestParseOpenPrs(unittest.TestCase):
    def test_parse(self):
        pulls = [{"number": 7, "head": {"sha": "abc"}},
                 {"number": 8, "head": {"sha": "def"}}]
        self.assertEqual(parse_open_prs(pulls, "o/r"),
                         [OpenPR("o/r", 7, "abc"), OpenPR("o/r", 8, "def")])

    def test_skips_incomplete(self):
        pulls = [{"number": 7}, {"head": {"sha": "x"}}, {"number": 9, "head": {"sha": "z"}}]
        self.assertEqual(parse_open_prs(pulls, "o/r"), [OpenPR("o/r", 9, "z")])


class TestHasCompletedReview(unittest.TestCase):
    def test_reflects_store_done(self):
        store = StateStore(":memory:")
        e = Event("d1", "o/r", 7, "abc", "/review")
        store.record_received(e)
        for s in (State.QUEUED, State.PROCESSING, State.DONE):
            store.transition("d1", s)
        hcr = make_has_completed_review(store)
        self.assertTrue(hcr("o/r", 7, "abc", "/review"))
        self.assertFalse(hcr("o/r", 7, "def", "/review"))     # иной head_sha
        self.assertFalse(hcr("o/r", 7, "abc", "/describe"))   # иная команда


if __name__ == "__main__":
    unittest.main()
