"""Порты sweeper (go-live): parse_open_prs + has_completed_review (store + verify)."""
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


class TestSwallowedFailureVerify(unittest.TestCase):
    def _store_done(self):
        store = StateStore(":memory:")
        e = Event("d1", "o/r", 7, "abc", "/review")
        store.record_received(e)
        for s in (State.QUEUED, State.PROCESSING, State.DONE):
            store.transition("d1", s)
        return store

    def test_no_done_row_short_circuits_without_verify_call(self):
        store = StateStore(":memory:")            # пусто → нет DONE-строки
        calls = []
        hcr = make_has_completed_review(store, verify=lambda *a: calls.append(a) or True)
        self.assertFalse(hcr("o/r", 7, "abc", "/review"))  # reconcile
        self.assertEqual(calls, [])                # verify не звали: и так не done

    def test_done_row_confirmed_by_github(self):
        hcr = make_has_completed_review(self._store_done(), verify=lambda *a: True)
        self.assertTrue(hcr("o/r", 7, "abc", "/review"))   # артефакт есть → done

    def test_done_row_but_swallowed_failure_triggers_reconcile(self):
        # DONE в сторе, но на GitHub артефакта нет → проглоченный сбой → reconcile
        hcr = make_has_completed_review(self._store_done(), verify=lambda *a: False)
        self.assertFalse(hcr("o/r", 7, "abc", "/review"))


if __name__ == "__main__":
    unittest.main()
