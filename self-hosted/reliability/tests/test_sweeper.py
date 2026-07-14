"""СТ-13, 29, 30, 31, 32: reconciliation sweeper."""
import unittest

from reliability import metrics
from reliability.state import Event, State, StateStore
from reliability.sweeper import OpenPR, business_key, sweep


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeClient:
    def __init__(self):
        self.calls = []

    def post_issue_comment(self, repo, number, body):
        self.calls.append((repo, number, body))


class Enq:
    def __init__(self):
        self.calls = []

    def __call__(self, event, *, force=False):
        self.calls.append((event, force))


def make_event(did="d1"):
    return Event(delivery_id=did, repo="o/r", number=7, head_sha="abc", command="/review")


class TestSweep(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = StateStore(":memory:", clock=self.clock)
        self.client = FakeClient()
        self.enq = Enq()
        metrics.reset()

    def _sweep(self, prs=(), reviewed=True, max_attempts=5, max_cycles=3, stale_deadline=60):
        return sweep(self.store,
                     list_open_prs=lambda: list(prs),
                     has_completed_review=lambda *a: reviewed,
                     enqueue=self.enq, client=self.client,
                     commands=["/review"], stale_deadline=stale_deadline,
                     max_attempts=max_attempts, max_cycles=max_cycles)

    def _stuck(self, did="d1", attempts=0, failed=False):
        e = make_event(did)
        self.store.record_received(e)
        self.store.transition(did, State.QUEUED)
        self.store.transition(did, State.PROCESSING)
        if failed:
            self.store.transition(did, State.FAILED)
        for _ in range(attempts):
            self.store.increment_attempt(did)
        self.clock.t += 1000  # старше deadline

    # СТ-13: застрявшее (PROCESSING) ниже порога → claim в очередь + повтор
    def test_stale_below_max_requeues(self):
        self._stuck(attempts=0)
        rep = self._sweep(max_attempts=5)
        self.assertEqual(self.store.state_of("d1"), State.QUEUED)  # переиспользуем строку
        self.assertEqual(len(rep.requeued), 1)
        event, force = self.enq.calls[0]
        self.assertEqual(event.delivery_id, "d1")  # та же строка, не орфан
        self.assertFalse(force)
        self.assertEqual(metrics.get("reconcile_requeues"), 1)

    # СТ-13 регресс: застрявшее в FAILED ниже порога — НЕ падает, идёт в очередь
    def test_stale_failed_below_max_requeues(self):
        self._stuck(attempts=0, failed=True)
        rep = self._sweep(max_attempts=5)
        self.assertEqual(self.store.state_of("d1"), State.QUEUED)
        self.assertEqual(len(rep.requeued), 1)

    # СТ-13 регресс: застрявшее в FAILED на пороге → dead-letter (не крэш)
    def test_stale_failed_at_max_dead_letters(self):
        self._stuck(attempts=5, failed=True)
        rep = self._sweep(max_attempts=5)
        self.assertEqual(self.store.state_of("d1"), State.DEAD_LETTER)
        self.assertEqual(len(rep.dead_lettered), 1)
        self.assertEqual(metrics.get("dead_letter_total"), 1)

    # СТ-13: застрявшее на пороге → dead-letter + коммент + метрика
    def test_stale_at_max_dead_letters(self):
        self._stuck(attempts=5)
        rep = self._sweep(max_attempts=5)
        self.assertEqual(self.store.state_of("d1"), State.DEAD_LETTER)
        self.assertEqual(len(rep.dead_lettered), 1)
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(metrics.get("dead_letter_total"), 1)
        self.assertEqual(self.enq.calls, [])

    # СТ-29: PR с подтверждённым ревью → ничего
    def test_pr_with_review_no_action(self):
        rep = self._sweep(prs=[OpenPR("o/r", 7, "abc")], reviewed=True)
        self.assertEqual(self.enq.calls, [])
        self.assertEqual(rep.reconciled, [])

    # СТ-29/31: PR без ревью, не в работе → reconcile-enqueue (force)
    def test_pr_without_review_reconciles(self):
        rep = self._sweep(prs=[OpenPR("o/r", 7, "abc")], reviewed=False)
        self.assertEqual(len(rep.reconciled), 1)
        event, force = self.enq.calls[0]
        self.assertTrue(force)
        self.assertEqual(event.event_type, "reconcile")
        self.assertEqual(self.store.reconcile_cycles(business_key("o/r", 7, "abc", "/review")), 1)

    # СТ-30: PR без ревью, но событие уже в работе → не дублируем
    def test_in_flight_not_duplicated(self):
        self.store.record_received(make_event("existing"))  # RECEIVED — в работе
        rep = self._sweep(prs=[OpenPR("o/r", 7, "abc")], reviewed=False)
        self.assertEqual(rep.reconciled, [])
        self.assertEqual(self.enq.calls, [])

    # СТ-32: эскалация после max_cycles, один коммент, дальше молча стоп
    def test_escalation_after_max_cycles(self):
        bkey = business_key("o/r", 7, "abc", "/review")
        for _ in range(3):
            self.store.bump_reconcile(bkey)  # cycles=3=max
        rep = self._sweep(prs=[OpenPR("o/r", 7, "abc")], reviewed=False, max_cycles=3)
        self.assertEqual(len(rep.escalated), 1)
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(metrics.get("reconcile_escalated_total"), 1)
        self.assertEqual(self.enq.calls, [])
        # повторный проход — новых комментов нет
        rep2 = self._sweep(prs=[OpenPR("o/r", 7, "abc")], reviewed=False, max_cycles=3)
        self.assertEqual(rep2.escalated, [])
        self.assertEqual(len(self.client.calls), 1)


if __name__ == "__main__":
    unittest.main()
