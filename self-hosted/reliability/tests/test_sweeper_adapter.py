"""Порты sweeper (go-live): parse_open_prs + has_completed_review (store + verify)."""
import unittest

from reliability.sweeper import OpenPR
from reliability.sweeper_adapter import (
    make_has_completed_review,
    make_list_open_prs_all,
    make_list_open_prs_masked,
    parse_open_prs,
    parse_repo_specs,
    resolve_masked_repos,
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


class TestParseRepoSpecs(unittest.TestCase):
    def test_splits_concrete_masks_and_star(self):
        concrete, masks = parse_repo_specs(
            ["po-helper-org/poh-pr-agents", "ai-oxudevelopment/*", " *", "", "  "])
        self.assertEqual(concrete, ["po-helper-org/poh-pr-agents"])
        self.assertEqual(masks, ["ai-oxudevelopment", "*"])

    def test_all_concrete(self):
        self.assertEqual(parse_repo_specs(["o/a", "o/b"]), (["o/a", "o/b"], []))

    def test_only_masks(self):
        self.assertEqual(parse_repo_specs(["po-helper-org/*", "ai-oxudevelopment/*"]),
                         ([], ["po-helper-org", "ai-oxudevelopment"]))

    def test_bare_owner_without_slash_is_mask(self):
        # ровно то, что пользователь задал изначально (`owner` без `/repo`) и получил
        # 401-цикл: теперь трактуется как маска owner/*, а не как невалидный owner/repo.
        self.assertEqual(parse_repo_specs(["po-helper-org", "ai-oxudevelopment"]),
                         ([], ["po-helper-org", "ai-oxudevelopment"]))


class TestResolveMaskedRepos(unittest.TestCase):
    def _fixture(self):
        provider = FakeProvider([
            {"id": 11, "account": {"login": "po-helper-org"}},
            {"id": 22, "account": {"login": "ai-oxudevelopment"}},
            {"id": 33, "account": {"login": "kibarik"}},
        ])
        client = FakeClient(
            repos_by_token={"tok-11": ["po-helper-org/a", "po-helper-org/b"],
                            "tok-22": ["ai-oxudevelopment/x"],
                            "tok-33": ["kibarik/mts-po-workspace"]},
            pulls_by_repo={})
        return provider, client

    def test_no_masks_returns_empty_without_calls(self):
        provider, client = self._fixture()
        self.assertEqual(resolve_masked_repos([], provider, client), [])
        self.assertEqual(provider.token_calls, [])  # без масок сеть не трогаем

    def test_expands_only_requested_owners(self):
        provider, client = self._fixture()
        repos = resolve_masked_repos(["po-helper-org", "ai-oxudevelopment"], provider, client)
        self.assertEqual(repos, ["po-helper-org/a", "po-helper-org/b", "ai-oxudevelopment/x"])
        self.assertEqual(provider.token_calls, [11, 22])  # kibarik не запрашивали

    def test_owner_match_is_case_insensitive(self):
        provider, client = self._fixture()
        self.assertEqual(resolve_masked_repos(["PO-Helper-Org"], provider, client),
                         ["po-helper-org/a", "po-helper-org/b"])

    def test_unknown_owner_skipped(self):
        provider, client = self._fixture()
        self.assertEqual(resolve_masked_repos(["nonexistent"], provider, client), [])
        self.assertEqual(provider.token_calls, [])  # App не установлен → пропуск, не падаем

    def test_star_expands_all_installations(self):
        provider, client = self._fixture()
        repos = resolve_masked_repos(["*"], provider, client)
        self.assertEqual(sorted(repos),
                         ["ai-oxudevelopment/x", "kibarik/mts-po-workspace",
                          "po-helper-org/a", "po-helper-org/b"])
        self.assertEqual(provider.token_calls, [11, 22, 33])


class TestListOpenPrsMasked(unittest.TestCase):
    def test_mask_plus_concrete_dedup_and_open_pulls(self):
        # маска `po-helper-org/*` раскрывается в реальные репо орг; отдельно указан
        # точный репо из другой орг; пересекающийся репо не дублируется.
        provider = FakeProvider([{"id": 11, "account": {"login": "po-helper-org"}}])
        client = FakeClient(
            repos_by_token={"tok-11": ["po-helper-org/poh-pr-agents", "po-helper-org/other"]},
            pulls_by_repo={
                "po-helper-org/poh-pr-agents": [{"number": 1, "head": {"sha": "s1"}}],
                "po-helper-org/other": [{"number": 2, "head": {"sha": "s2"}}],
                "ai-oxudevelopment/z": [{"number": 3, "head": {"sha": "s3"}}],
            })
        # точный дубль раскрытого репо + маска + другой точный репо
        repos = ["po-helper-org/poh-pr-agents", "po-helper-org/*", "ai-oxudevelopment/z"]
        prs = make_list_open_prs_masked(client, provider, repos)()
        self.assertEqual(sorted((p.repo, p.number) for p in prs),
                         [("ai-oxudevelopment/z", 3), ("po-helper-org/other", 2),
                          ("po-helper-org/poh-pr-agents", 1)])

    def test_reresolves_each_call_picks_up_new_repo(self):
        # свежее раскрытие на каждом проходе → новый репо орг подхватывается сам
        provider = FakeProvider([{"id": 11, "account": {"login": "po-helper-org"}}])
        client = FakeClient(
            repos_by_token={"tok-11": ["po-helper-org/a"]},
            pulls_by_repo={"po-helper-org/a": [{"number": 1, "head": {"sha": "s1"}}],
                           "po-helper-org/b": [{"number": 2, "head": {"sha": "s2"}}]})
        list_open_prs = make_list_open_prs_masked(client, provider, ["po-helper-org/*"])
        self.assertEqual([(p.repo, p.number) for p in list_open_prs()], [("po-helper-org/a", 1)])
        client.repos_by_token["tok-11"] = ["po-helper-org/a", "po-helper-org/b"]  # орг вырос
        self.assertEqual(sorted((p.repo, p.number) for p in list_open_prs()),
                         [("po-helper-org/a", 1), ("po-helper-org/b", 2)])


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
