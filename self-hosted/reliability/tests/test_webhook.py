"""СТ-8: разбор webhook → Event."""
import unittest

from reliability.webhook import parse_events


def pr_payload(action="opened"):
    return {
        "action": action,
        "repository": {"full_name": "o/r"},
        "pull_request": {"number": 7, "head": {"sha": "abc"}},
    }


class TestParseEvents(unittest.TestCase):
    def test_pr_open_produces_commands(self):
        evs = parse_events("pull_request", "del1", pr_payload("opened"),
                           pr_commands=("/describe", "/review"))
        self.assertEqual([e.command for e in evs], ["/describe", "/review"])
        for e in evs:
            self.assertEqual((e.repo, e.number, e.head_sha), ("o/r", 7, "abc"))
        self.assertEqual(evs[0].delivery_id, "del1:/describe")  # уникальный ключ покомандно

    def test_pr_non_trigger_action_ignored(self):
        self.assertEqual(parse_events("pull_request", "d", pr_payload("labeled")), [])

    def test_pr_synchronize_triggers(self):
        evs = parse_events("pull_request", "d", pr_payload("synchronize"))
        self.assertTrue(evs)

    def test_issue_comment_slash_command(self):
        p = {"action": "created", "repository": {"full_name": "o/r"},
             "issue": {"number": 9}, "comment": {"body": "/review please"}}
        evs = parse_events("issue_comment", "d", p)
        self.assertEqual(len(evs), 1)
        self.assertEqual((evs[0].command, evs[0].number), ("/review", 9))

    def test_issue_comment_non_slash_ignored(self):
        p = {"action": "created", "repository": {"full_name": "o/r"},
             "issue": {"number": 9}, "comment": {"body": "просто текст"}}
        self.assertEqual(parse_events("issue_comment", "d", p), [])

    def test_unknown_event_ignored(self):
        self.assertEqual(parse_events("push", "d", {}), [])


if __name__ == "__main__":
    unittest.main()
