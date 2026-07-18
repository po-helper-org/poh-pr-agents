"""Порты sweeper (go-live): parse_open_prs + has_completed_review (store + verify)."""
import unittest

from reliability.sweeper import OpenPR
from reliability.sweeper_adapter import (
    make_has_completed_review,
    make_list_open_prs_all,
    parse_open_prs,
)
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


class FakeProvider:
    def __init__(self, installations):
        self.installations = installations
        self.token_calls = []

    def list_installations(self):
        return self.installations

    def token_for(self, inst_id):
        self.token_calls.append(inst_id)
        return f"tok-{inst_id}"


class FakeClient:
    def __init__(self, repos_by_token, pulls_by_repo):
        self.repos_by_token = repos_by_token
        self.pulls_by_repo = pulls_by_repo

    def list_installation_repos(self, token):
        return self.repos_by_token[token]

    def list_open_pulls(self, repo):
        return self.pulls_by_repo.get(repo, [])


class TestListOpenPrsAll(unittest.TestCase):
    def test_walks_all_installations_and_repos(self):
        # две установки (две орг/аккаунта) → все их репозитории обходятся
        provider = FakeProvider([{"id": 11}, {"id": 22}])
        client = FakeClient(
            repos_by_token={"tok-11": ["po-helper-org/a", "po-helper-org/b"],
                            "tok-22": ["kibarik/mts-po-workspace"]},
            pulls_by_repo={
                "po-helper-org/a": [{"number": 1, "head": {"sha": "s1"}}],
                "po-helper-org/b": [],
                "kibarik/mts-po-workspace": [{"number": 9, "head": {"sha": "s9"}}],
            })
        prs = make_list_open_prs_all(client, provider)()
        self.assertEqual(sorted((p.repo, p.number) for p in prs),
                         [("kibarik/mts-po-workspace", 9), ("po-helper-org/a", 1)])
        self.assertEqual(provider.token_calls, [11, 22])  # токен на каждую установку


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
