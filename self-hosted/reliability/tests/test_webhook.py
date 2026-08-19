"""СТ-8: разбор webhook → Event + обогащение head_sha."""
import unittest

from reliability.webhook import enrich_events, parse_events, stop_labels_present


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


class TestEnrichHeadSha(unittest.TestCase):
    def _issue_comment_event(self):
        p = {"action": "created", "repository": {"full_name": "o/r"},
             "issue": {"number": 9}, "comment": {"body": "/review"}}
        (e,) = parse_events("issue_comment", "d", p)
        self.assertEqual(e.head_sha, "")  # в payload sha нет
        return e

    def test_fills_missing_head_sha(self):
        e = self._issue_comment_event()
        out = enrich_events([e], lambda repo, num: "sha9")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].head_sha, "sha9")
        self.assertEqual(out[0].business_key, "o/r#9@sha9:/review")

    def test_drops_event_when_not_a_pr(self):
        e = self._issue_comment_event()
        out = enrich_events([e], lambda repo, num: "")  # issue — не PR
        self.assertEqual(out, [])  # неполный ключ хуже отсутствия события

    def test_fetch_error_drops_event_not_raises(self):
        # транзиентная 5xx при обогащении не должна ронять батч в 500 (риск К-1)
        e = self._issue_comment_event()

        def boom(repo, num):
            raise RuntimeError("github 503")

        self.assertEqual(enrich_events([e], boom), [])   # отброшено, не исключение

    def test_pr_event_passes_without_api_call(self):
        (e,) = parse_events("pull_request", "d", pr_payload("opened"),
                            pr_commands=("/review",))
        calls = []

        def fetch(repo, num):
            calls.append((repo, num))
            return "should-not-be-used"

        out = enrich_events([e], fetch)
        self.assertEqual(out[0].head_sha, "abc")  # исходный sha сохранён
        self.assertEqual(calls, [])               # к API не ходили


if __name__ == "__main__":
    unittest.main()


class TestStopLabels(unittest.TestCase):
    """Протокол v1: pr-closer:closed и agents:off останавливают агентов.

    Без этой проверки инвариант «после стоп-слова ни один агент не пишет
    в эту ветку» держался бы на добросовестности, а не на механизме: PR-Closer
    метку ставит, но сам себя ею не остановит.
    """

    def labeled_pr(self, *names, action="opened"):
        payload = pr_payload(action)
        payload["pull_request"]["labels"] = [{"name": n} for n in names]
        return payload

    def labeled_comment(self, *names, body="/review"):
        return {
            "action": "created",
            "repository": {"full_name": "o/r"},
            "issue": {"number": 7, "labels": [{"name": n} for n in names]},
            "comment": {"body": body},
        }

    def test_closed_label_stops_pr_event(self):
        self.assertEqual(parse_events("pull_request", "d", self.labeled_pr("pr-closer:closed")), [])

    def test_agents_off_stops_pr_event(self):
        self.assertEqual(parse_events("pull_request", "d", self.labeled_pr("agents:off")), [])

    def test_explicit_command_is_stopped_too(self):
        # Рубильник, который обходится явной командой, рубильником не является.
        self.assertEqual(parse_events("issue_comment", "d", self.labeled_comment("agents:off")), [])

    def test_other_labels_do_not_stop(self):
        evs = parse_events("pull_request", "d", self.labeled_pr("enhancement", "Review effort 1/5"))
        self.assertTrue(evs)

    def test_no_labels_key_is_not_a_stop(self):
        self.assertTrue(parse_events("pull_request", "d", pr_payload("opened")))

    def test_labels_are_read_from_both_shapes(self):
        self.assertEqual(stop_labels_present(self.labeled_pr("agents:off")), {"agents:off"})
        self.assertEqual(stop_labels_present(self.labeled_comment("pr-closer:closed")),
                         {"pr-closer:closed"})
        self.assertEqual(stop_labels_present({}), frozenset())

    def test_removing_the_label_returns_the_pr_to_work(self):
        # Снятие метки — осознанное действие человека, и оно должно работать
        # без перезапуска чего-либо.
        stopped = self.labeled_pr("agents:off", action="synchronize")
        self.assertEqual(parse_events("pull_request", "d", stopped), [])
        stopped["pull_request"]["labels"] = []
        self.assertTrue(parse_events("pull_request", "d", stopped))
