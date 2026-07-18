"""СТ-25: идемпотентная публикация — upsert (create/update/пагинация/дедуп/bot-фильтр)."""
import json
import re
import unittest

from reliability.github_client import GitHubAppClient

MARKER = "<!-- reliability:failure:/review -->"


def bot_comment(cid, marker=MARKER, extra=""):
    return {"id": cid, "body": f"текст{extra}\n\n{marker}", "user": {"type": "Bot"}}


class PagingTransport:
    """GET отдаёт страницы по 100; POST/PATCH/DELETE — 2xx. Пишет вызовы."""
    def __init__(self, pages=None):
        self.pages = pages if pages is not None else [[]]
        self.calls = []  # (method, url)

    def __call__(self, method, url, data, headers):
        self.calls.append((method, url))
        if method == "GET":
            p = int(re.search(r"[?&]page=(\d+)", url).group(1))  # не матчить per_page
            items = self.pages[p - 1] if p - 1 < len(self.pages) else []
            return 200, json.dumps(items).encode()
        return 200, b"{}"

    def methods(self):
        return [c[0] for c in self.calls]


def client_with(transport):
    return GitHubAppClient(token_provider=lambda repo: "tok", transport=transport)


class TestUpsertComment(unittest.TestCase):
    def test_creates_when_no_existing(self):
        t = PagingTransport([[]])
        client_with(t).upsert_comment("o/r", 7, MARKER, "hello")
        self.assertEqual(t.methods(), ["GET", "POST"])

    def test_updates_when_marker_found(self):
        t = PagingTransport([[bot_comment(5)]])
        client_with(t).upsert_comment("o/r", 7, MARKER, "новый")
        self.assertEqual(t.methods(), ["GET", "PATCH"])
        patch = [c for c in t.calls if c[0] == "PATCH"][0]
        self.assertIn("/issues/comments/5", patch[1])

    def test_finds_marker_on_second_page(self):
        # СТ-25 на нагруженном PR: наш коммент за пределами первой страницы
        page1 = [bot_comment(i, marker="<!-- other -->") for i in range(100)]
        page2 = [bot_comment(777)]
        t = PagingTransport([page1, page2])
        client_with(t).upsert_comment("o/r", 7, MARKER, "x")
        self.assertEqual(t.methods(), ["GET", "GET", "PATCH"])  # не создали дубль
        self.assertIn("/issues/comments/777", [c[1] for c in t.calls if c[0] == "PATCH"][0])

    def test_dedup_deletes_extras(self):
        t = PagingTransport([[bot_comment(5), bot_comment(6)]])  # гонка создала два
        client_with(t).upsert_comment("o/r", 7, MARKER, "x")
        self.assertEqual(t.methods(), ["GET", "PATCH", "DELETE"])  # первый правим, лишний удаляем

    def test_foreign_marker_quote_ignored(self):
        # чужой пользователь процитировал маркер → не матчим (иначе PATCH→403)
        foreign = {"id": 9, "body": f"смотри {MARKER}", "user": {"type": "User"}}
        t = PagingTransport([[foreign]])
        client_with(t).upsert_comment("o/r", 7, MARKER, "x")
        self.assertEqual(t.methods(), ["GET", "POST"])  # создаём свой, чужой не трогаем

    def test_other_marker_does_not_match(self):
        t = PagingTransport([[bot_comment(5, marker="<!-- reliability:failure:/describe -->")]])
        client_with(t).upsert_comment("o/r", 7, MARKER, "x")
        self.assertEqual(t.methods(), ["GET", "POST"])

    def test_list_open_pulls_paginates(self):
        page1 = [{"number": i, "head": {"sha": f"s{i}"}} for i in range(100)]
        page2 = [{"number": 100, "head": {"sha": "s100"}}]
        t = PagingTransport([page1, page2])
        pulls = client_with(t).list_open_pulls("o/r")
        self.assertEqual(len(pulls), 101)
        self.assertEqual(t.methods(), ["GET", "GET"])

    def test_list_error_raises(self):
        class BadGet(PagingTransport):
            def __call__(self, method, url, data, headers):
                return (500, b"err")

        with self.assertRaises(RuntimeError):
            client_with(BadGet()).upsert_comment("o/r", 7, MARKER, "x")


class TestGetPullHeadSha(unittest.TestCase):
    def _client(self, status, body):
        def transport(method, url, data, headers):
            self.assertEqual(method, "GET")
            self.assertIn("/pulls/9", url)
            return status, body
        return GitHubAppClient(token_provider=lambda repo: "tok", transport=transport)

    def test_returns_head_sha(self):
        c = self._client(200, json.dumps({"head": {"sha": "s9"}}).encode())
        self.assertEqual(c.get_pull_head_sha("o/r", 9), "s9")

    def test_404_is_empty_not_error(self):
        # номер оказался issue, а не PR → пусто (обогащение отбросит событие)
        c = self._client(404, b'{"message":"Not Found"}')
        self.assertEqual(c.get_pull_head_sha("o/r", 9), "")

    def test_other_error_raises(self):
        c = self._client(500, b"boom")
        with self.assertRaises(RuntimeError):
            c.get_pull_head_sha("o/r", 9)


class TestHasBotActivity(unittest.TestCase):
    def test_true_when_bot_comment_present(self):
        t = PagingTransport([[bot_comment(5)]])
        self.assertTrue(client_with(t).has_bot_activity("o/r", 7))

    def test_false_when_only_human_comments(self):
        human = {"id": 1, "body": "hi", "user": {"type": "User"}}
        t = PagingTransport([[human]])
        self.assertFalse(client_with(t).has_bot_activity("o/r", 7))

    def test_false_when_no_comments(self):
        t = PagingTransport([[]])
        self.assertFalse(client_with(t).has_bot_activity("o/r", 7))

    def test_finds_bot_on_second_page(self):
        page1 = [{"id": i, "body": "x", "user": {"type": "User"}} for i in range(100)]
        page2 = [bot_comment(777)]
        t = PagingTransport([page1, page2])
        self.assertTrue(client_with(t).has_bot_activity("o/r", 7))
        self.assertEqual(t.methods(), ["GET", "GET"])


class TestListInstallationRepos(unittest.TestCase):
    def _client(self, pages):
        # /installation/repositories отдаёт {"repositories":[...]} постранично
        def transport(method, url, data, headers):
            self.assertEqual(method, "GET")
            self.assertIn("/installation/repositories", url)
            p = int(re.search(r"[?&]page=(\d+)", url).group(1))
            items = pages[p - 1] if p - 1 < len(pages) else []
            return 200, json.dumps({"repositories": items}).encode()
        return GitHubAppClient(token_provider=lambda repo: "x", transport=transport)

    def test_collects_full_names(self):
        c = self._client([[{"full_name": "org/a"}, {"full_name": "org/b"}]])
        self.assertEqual(c.list_installation_repos("inst-tok"), ["org/a", "org/b"])

    def test_paginates(self):
        page1 = [{"full_name": f"org/r{i}"} for i in range(100)]
        page2 = [{"full_name": "org/last"}]
        c = self._client([page1, page2])
        self.assertEqual(len(c.list_installation_repos("t")), 101)


if __name__ == "__main__":
    unittest.main()
