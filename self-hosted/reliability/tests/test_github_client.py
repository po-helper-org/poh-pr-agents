"""СТ-25: идемпотентная публикация — upsert комментария (create/update по маркеру)."""
import json
import unittest

from reliability.github_client import GitHubAppClient

MARKER = "<!-- reliability:failure:/review -->"


class FakeTransport:
    def __init__(self, list_body=b"[]"):
        self.list_body = list_body
        self.calls = []  # (method, url, data)

    def __call__(self, method, url, data, headers):
        self.calls.append((method, url, data))
        if method == "GET":
            return 200, self.list_body
        return 201, b"{}"

    def methods(self):
        return [c[0] for c in self.calls]


def client_with(transport):
    return GitHubAppClient(token_provider=lambda repo: "tok", transport=transport)


class TestUpsertComment(unittest.TestCase):
    def test_creates_when_no_existing(self):
        t = FakeTransport(list_body=b"[]")
        client_with(t).upsert_comment("o/r", 7, MARKER, "hello")
        self.assertEqual(t.methods(), ["GET", "POST"])
        post = [c for c in t.calls if c[0] == "POST"][0]
        self.assertIn("/repos/o/r/issues/7/comments", post[1])
        self.assertIn(MARKER, json.loads(post[2])["body"])  # маркер вшит в тело

    def test_updates_when_marker_found(self):
        t = FakeTransport(list_body=json.dumps(
            [{"id": 5, "body": f"старый текст\n\n{MARKER}"}]).encode())
        client_with(t).upsert_comment("o/r", 7, MARKER, "новый текст")
        self.assertEqual(t.methods(), ["GET", "PATCH"])
        patch = [c for c in t.calls if c[0] == "PATCH"][0]
        self.assertIn("/issues/comments/5", patch[1])  # правим существующий
        self.assertIn("новый текст", json.loads(patch[2])["body"])

    def test_other_marker_does_not_match(self):
        t = FakeTransport(list_body=json.dumps(
            [{"id": 5, "body": "<!-- reliability:failure:/describe -->"}]).encode())
        client_with(t).upsert_comment("o/r", 7, MARKER, "x")
        self.assertEqual(t.methods(), ["GET", "POST"])  # другой маркер → создаём свой

    def test_error_status_raises(self):
        class BadGet(FakeTransport):
            def __call__(self, method, url, data, headers):
                return (500, b"err")

        with self.assertRaises(RuntimeError):
            client_with(BadGet()).upsert_comment("o/r", 7, MARKER, "x")


if __name__ == "__main__":
    unittest.main()
