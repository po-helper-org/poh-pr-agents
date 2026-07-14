"""СТ-27 delivery: построение и отправка запроса на публикацию комментария."""
import json
import unittest

from reliability.github_client import GitHubAppClient


class FakeTransport:
    def __init__(self, status=201, body=b"{}"):
        self.status, self.body, self.calls = status, body, []

    def __call__(self, url, data, headers):
        self.calls.append((url, data, headers))
        return self.status, self.body


class TestGitHubAppClient(unittest.TestCase):
    def test_post_comment_builds_correct_request(self):
        t = FakeTransport()
        client = GitHubAppClient(token_provider=lambda repo: "tok123", transport=t)
        client.post_issue_comment("o/r", 7, "hello")
        url, data, headers = t.calls[0]
        self.assertTrue(url.endswith("/repos/o/r/issues/7/comments"))
        self.assertEqual(json.loads(data)["body"], "hello")
        self.assertEqual(headers["Authorization"], "Bearer tok123")

    def test_error_status_raises(self):
        t = FakeTransport(status=403, body=b"forbidden")
        client = GitHubAppClient(token_provider=lambda repo: "tok", transport=t)
        with self.assertRaises(RuntimeError):
            client.post_issue_comment("o/r", 7, "hi")


if __name__ == "__main__":
    unittest.main()
