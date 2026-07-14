"""СТ-27: видимое оповещение о провале в PR/issue."""
import unittest

from reliability.notifier import build_failure_comment, notify_failure
from reliability.state import Event


class FakeClient:
    def __init__(self):
        self.calls = []

    def post_issue_comment(self, repo, number, body):
        self.calls.append((repo, number, body))


def make_event():
    return Event(delivery_id="d1", repo="o/r", number=7, head_sha="abc", command="/review")


class TestFailureComment(unittest.TestCase):
    def test_escalated_comment_mentions_cause_command_and_deadletter(self):
        body = build_failure_comment("/review", "RateLimitError", attempts=5, escalated=True)
        self.assertIn("/review", body)
        self.assertIn("RateLimitError", body)
        self.assertIn("5", body)
        self.assertIn("dead-letter", body)

    def test_non_escalated_comment_mentions_retry(self):
        body = build_failure_comment("/review", "TimeoutError", attempts=1, escalated=False)
        self.assertIn("повтор", body.lower())
        self.assertNotIn("dead-letter", body)

    def test_notify_failure_posts_to_pr_and_returns_body(self):
        client = FakeClient()
        err = TimeoutError("upstream stalled")
        body = notify_failure(client, make_event(), err, attempts=3, escalated=True)
        self.assertEqual(len(client.calls), 1)
        repo, number, posted = client.calls[0]
        self.assertEqual((repo, number), ("o/r", 7))
        self.assertEqual(posted, body)
        self.assertIn("TimeoutError", posted)


if __name__ == "__main__":
    unittest.main()
