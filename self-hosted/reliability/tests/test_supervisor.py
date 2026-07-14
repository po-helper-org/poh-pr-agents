"""СТ-14..16, 27: оркестрация обработки события супервизором."""
import unittest

from reliability import metrics
from reliability.state import Event, State, StateStore
from reliability.supervisor import process


class FakeAnalyze:
    def __init__(self, exc=None):
        self.exc, self.calls = exc, 0

    def __call__(self, event):
        self.calls += 1
        if self.exc:
            raise self.exc


class FakeClient:
    def __init__(self):
        self.calls = []

    def post_issue_comment(self, repo, number, body):
        self.calls.append((repo, number, body))


def ev(delivery="d1", head="abc", command="/review"):
    return Event(delivery_id=delivery, repo="o/r", number=7, head_sha=head, command=command)


class TestProcess(unittest.TestCase):
    def setUp(self):
        self.store = StateStore(":memory:")
        self.client = FakeClient()
        metrics.reset()

    def test_success_marks_done_without_comment(self):
        e = ev()
        self.store.record_received(e)
        a = FakeAnalyze()
        r = process(e, a, self.store, self.client, max_attempts=5)
        self.assertEqual(r.state, State.DONE)
        self.assertEqual(a.calls, 1)
        self.assertEqual(self.client.calls, [])

    def test_failure_below_threshold_no_comment(self):
        e = ev()
        self.store.record_received(e)
        a = FakeAnalyze(exc=TimeoutError("stall"))
        r = process(e, a, self.store, self.client, max_attempts=5)
        self.assertEqual(r.state, State.FAILED)
        self.assertEqual(self.client.calls, [])  # ещё не эскалировано

    def test_failure_at_threshold_dead_letters_and_comments(self):
        e = ev()
        self.store.record_received(e)
        a = FakeAnalyze(exc=RuntimeError("boom"))
        r = process(e, a, self.store, self.client, max_attempts=1)
        self.assertEqual(r.state, State.DEAD_LETTER)
        self.assertTrue(r.notified)
        self.assertEqual(len(self.client.calls), 1)  # СТ-27: видимый коммент
        repo, number, body = self.client.calls[0]
        self.assertEqual((repo, number), ("o/r", 7))
        self.assertIn("RuntimeError", body)
        self.assertIn("dead-letter", body)
        self.assertEqual(metrics.get("dead_letter_total"), 1)  # СТ-27(б)

    def test_base_exception_propagates_without_comment(self):
        # отмена/сигнал (KeyboardInterrupt — BaseException, не Exception) не глотаем
        e = ev()
        self.store.record_received(e)
        a = FakeAnalyze(exc=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            process(e, a, self.store, self.client, max_attempts=1)
        self.assertEqual(self.client.calls, [])          # не постим при отмене
        self.assertEqual(metrics.get("dead_letter_total"), 0)

    def test_already_done_business_key_skips_analysis(self):
        # первый delivery делает работу
        a1 = ev(delivery="a:/review")
        self.store.record_received(a1)
        process(a1, FakeAnalyze(), self.store, self.client, max_attempts=5)
        # второй delivery с тем же (repo,number,head,command) — не должен анализировать
        b1 = ev(delivery="b:/review")
        self.store.record_received(b1)
        spy = FakeAnalyze()
        r = process(b1, spy, self.store, self.client, max_attempts=5)
        self.assertEqual(r.state, State.DONE)
        self.assertTrue(r.skipped)
        self.assertEqual(spy.calls, 0)  # СТ-16: без повторной работы


if __name__ == "__main__":
    unittest.main()
