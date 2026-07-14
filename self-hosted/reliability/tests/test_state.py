"""СТ-2, 10, 11, 12, 13, 16, 28: машина состояний и идемпотентность."""
import unittest

from reliability.state import (
    Event,
    IllegalTransition,
    State,
    StateStore,
)


def make_event(delivery_id="d1", head_sha="abc", command="/review"):
    return Event(delivery_id=delivery_id, repo="o/r", number=7,
                 head_sha=head_sha, command=command)


class _Clock:
    """Управляемые часы для тестов stale/timestamp."""
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class TestStateStore(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.store = StateStore(":memory:", clock=self.clock)

    # СТ-2
    def test_record_received_dedups_duplicate_delivery(self):
        e = make_event()
        self.assertTrue(self.store.record_received(e))       # первый раз
        self.assertFalse(self.store.record_received(e))      # дубль доставки
        self.assertEqual(self.store.state_of("d1"), State.RECEIVED)

    # СТ-10: легальный путь
    def test_full_legal_path(self):
        self.store.record_received(make_event())
        self.store.transition("d1", State.QUEUED)
        self.store.transition("d1", State.PROCESSING)
        self.store.transition("d1", State.DONE)
        self.assertEqual(self.store.state_of("d1"), State.DONE)

    # СТ-10: нелегальный переход
    def test_illegal_transition_raises(self):
        self.store.record_received(make_event())
        with self.assertRaises(IllegalTransition):
            self.store.transition("d1", State.DONE)  # RECEIVED -> DONE запрещён

    # СТ-10: DONE терминально
    def test_done_is_terminal(self):
        self.store.record_received(make_event())
        self.store.transition("d1", State.QUEUED)
        self.store.transition("d1", State.PROCESSING)
        self.store.transition("d1", State.DONE)
        with self.assertRaises(IllegalTransition):
            self.store.transition("d1", State.QUEUED)

    # СТ-28: dead-letter не финал (можно повторно поставить)
    def test_dead_letter_can_requeue(self):
        self.store.record_received(make_event())
        self.store.transition("d1", State.FAILED)
        self.store.transition("d1", State.DEAD_LETTER)
        self.store.transition("d1", State.QUEUED)  # СТ-28
        self.assertEqual(self.store.state_of("d1"), State.QUEUED)

    # СТ-12
    def test_increment_attempt(self):
        self.store.record_received(make_event())
        self.assertEqual(self.store.increment_attempt("d1"), 1)
        self.assertEqual(self.store.increment_attempt("d1"), 2)

    # СТ-16: идемпотентный эффект по бизнес-ключу
    def test_already_done_by_business_key(self):
        e = make_event(head_sha="abc")
        self.store.record_received(e)
        self.store.transition("d1", State.QUEUED)
        self.store.transition("d1", State.PROCESSING)
        self.store.transition("d1", State.DONE)
        self.assertTrue(self.store.already_done(e.business_key))
        # новый head_sha — иной ключ, не считается сделанным (re-review нужен)
        other = make_event(delivery_id="d2", head_sha="def")
        self.assertFalse(self.store.already_done(other.business_key))

    # СТ-13: stale — застряло вне терминала дольше deadline
    def test_stale_detects_stuck_nonterminal(self):
        self.store.record_received(make_event())
        self.store.transition("d1", State.QUEUED)
        self.store.transition("d1", State.PROCESSING)
        self.clock.t += 100  # прошло 100 c
        stuck = self.store.stale(deadline_seconds=60)
        self.assertEqual([r["delivery_id"] for r in stuck], ["d1"])

    def test_stale_excludes_terminal(self):
        self.store.record_received(make_event())
        self.store.transition("d1", State.QUEUED)
        self.store.transition("d1", State.PROCESSING)
        self.store.transition("d1", State.DONE)
        self.clock.t += 100
        self.assertEqual(self.store.stale(deadline_seconds=60), [])


if __name__ == "__main__":
    unittest.main()
