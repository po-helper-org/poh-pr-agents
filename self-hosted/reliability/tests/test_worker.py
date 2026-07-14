"""СТ-14..18: worker — lease→process→ack/nack, retry/DLQ на очереди, коммент при DLQ."""
import unittest

from reliability import metrics
from reliability.queue import DurableQueue
from reliability.state import Event, State, StateStore, event_to_dict
from reliability.supervisor import process
from reliability.worker import TaskTimeout, handle_lease, run_once


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


def passthrough(fn, timeout):
    return fn()


def timeout_run(fn, timeout):
    raise TaskTimeout("simulated")


class TestWorker(unittest.TestCase):
    def setUp(self):
        self.store = StateStore(":memory:")
        self.queue = DurableQueue(":memory:")
        self.client = FakeClient()
        metrics.reset()

    def _enqueue(self, did="d1", cmd="/review", etype="pull_request"):
        e = Event(did, "o/r", 7, "abc", cmd, etype)
        self.store.record_received(e)
        self.queue.enqueue(event_to_dict(e), e.repo)
        return e

    def _handle(self, analyze, run_fn=passthrough, max_attempts=5):
        lease = self.queue.lease(visibility_timeout=30)
        return handle_lease(lease, queue=self.queue, store=self.store, client=self.client,
                            analyze=analyze, run_fn=run_fn, max_attempts=max_attempts)

    def test_success_acks(self):
        self._enqueue()
        out = self._handle(FakeAnalyze())
        self.assertEqual(out, "ack")
        self.assertEqual(self.queue.depth(), 0)
        self.assertEqual(self.store.state_of("d1"), State.DONE)
        self.assertEqual(metrics.get("processed_ok"), 1)

    def test_failure_requeues(self):
        self._enqueue()
        out = self._handle(FakeAnalyze(exc=RuntimeError("boom")), max_attempts=5)
        self.assertEqual(out, "requeued")
        self.assertEqual(self.queue.depth(), 1)              # вернулось в очередь
        self.assertEqual(self.store.state_of("d1"), State.FAILED)
        self.assertEqual(self.client.calls, [])              # ещё не эскалировано

    def test_failure_dead_letters_and_comments(self):
        self._enqueue()
        out = self._handle(FakeAnalyze(exc=RuntimeError("boom")), max_attempts=1)
        self.assertEqual(out, "dead_letter")
        self.assertEqual(self.store.state_of("d1"), State.DEAD_LETTER)
        self.assertEqual(len(self.client.calls), 1)          # СТ-27
        self.assertEqual(metrics.get("dead_letter_total"), 1)
        self.assertEqual(len(self.queue.dead_letters()), 1)

    def test_timeout_requeues(self):
        self._enqueue()
        out = self._handle(FakeAnalyze(), run_fn=timeout_run, max_attempts=5)
        self.assertEqual(out, "requeued")
        self.assertEqual(self.queue.depth(), 1)

    def test_reconcile_force_bypasses_already_done(self):
        # уже сделано другим delivery
        done = Event("a:/review", "o/r", 7, "abc", "/review")
        self.store.record_received(done)
        process(done, FakeAnalyze(), self.store)
        # reconcile-событие того же бизнес-ключа — force, анализ должен пойти
        self._enqueue(did="reconcile:x", etype="reconcile")
        spy = FakeAnalyze()
        out = self._handle(spy)
        self.assertEqual(out, "ack")
        self.assertEqual(spy.calls, 1)

    def test_run_once_empty_returns_false(self):
        self.assertFalse(run_once(self.queue, store=self.store, client=self.client,
                                  analyze=FakeAnalyze()))

    def test_run_once_processes_one(self):
        self._enqueue()
        self.assertTrue(run_once(self.queue, store=self.store, client=self.client,
                                 analyze=FakeAnalyze()))
        self.assertEqual(self.store.state_of("d1"), State.DONE)


if __name__ == "__main__":
    unittest.main()
