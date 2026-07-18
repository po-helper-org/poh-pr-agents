"""СТ-2, 10, 11, 12, 13, 16, 28: машина состояний и идемпотентность."""
import unittest
from unittest import mock

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

    # СТ-16: атомарный захват бизнес-ключа
    def test_try_claim_first_wins_second_loses(self):
        bk = "o/r#7@abc:/review"
        self.assertTrue(self.store.try_claim(bk, "a"))    # первый захватил
        self.assertFalse(self.store.try_claim(bk, "b"))   # второй — чужой держит
        self.assertEqual(self.store.claim_holder(bk), "a")

    def test_try_claim_is_reentrant_for_same_holder(self):
        bk = "o/r#7@abc:/review"
        self.assertTrue(self.store.try_claim(bk, "a"))
        self.assertTrue(self.store.try_claim(bk, "a"))    # тот же держатель — ок

    def test_release_claim_only_by_holder(self):
        bk = "o/r#7@abc:/review"
        self.store.try_claim(bk, "a")
        self.store.release_claim(bk, "b")                 # чужой не срывает захват
        self.assertEqual(self.store.claim_holder(bk), "a")
        self.store.release_claim(bk, "a")                 # держатель освобождает
        self.assertIsNone(self.store.claim_holder(bk))
        self.assertTrue(self.store.try_claim(bk, "b"))    # теперь можно пере-захватить

    # СТ-16: захват самозалечивается, если держатель уже терминален (утечка при
    # брошенном по таймауту process → dead-letter без release_claim). Иначе К-1:
    # reconcile-бэкстоп навсегда заблокирован.
    def test_try_claim_steals_stale_terminal_holder(self):
        holder = make_event(delivery_id="e1")            # bk = o/r#7@abc:/review
        bk = holder.business_key
        self.store.record_received(holder)
        self.store.try_claim(bk, "e1")                    # захват как во время анализа
        for s in (State.QUEUED, State.PROCESSING, State.FAILED, State.DEAD_LETTER):
            self.store.transition("e1", s)                # держатель dead-letter, release не звался
        self.assertEqual(self.store.claim_holder(bk), "e1")   # захват «протух»
        self.assertTrue(self.store.try_claim(bk, "e2"))       # сиблинг перехватывает
        self.assertEqual(self.store.claim_holder(bk), "e2")

    def test_try_claim_does_not_steal_inflight_holder(self):
        holder = make_event(delivery_id="e1")
        bk = holder.business_key
        self.store.record_received(holder)
        self.store.try_claim(bk, "e1")
        self.store.transition("e1", State.QUEUED)
        self.store.transition("e1", State.PROCESSING)    # держатель жив (in-flight)
        self.assertFalse(self.store.try_claim(bk, "e2"))  # НЕ перехватываем активного
        self.assertEqual(self.store.claim_holder(bk), "e1")

    # СТ-12
    def test_increment_attempt(self):
        self.store.record_received(make_event())
        self.assertEqual(self.store.increment_attempt("d1"), 1)
        self.assertEqual(self.store.increment_attempt("d1"), 2)

    def test_increment_attempt_unknown_raises(self):
        with self.assertRaises(KeyError):
            self.store.increment_attempt("nope")

    # СТ-10/17: CAS-защита transition от гонки (устаревшее чтение состояния)
    def test_transition_cas_rejects_stale_view(self):
        self.store.record_received(make_event())
        self.store.transition("d1", State.QUEUED)
        self.store.transition("d1", State.PROCESSING)
        self.store.transition("d1", State.DONE)  # реальное состояние в БД = done
        # смоделировать устаревшее чтение: get вернёт processing, а в БД уже done
        stale = {"state": State.PROCESSING.value}
        with mock.patch.object(self.store, "get", return_value=stale):
            with self.assertRaises(IllegalTransition):
                self.store.transition("d1", State.FAILED)  # CAS не найдёт processing
        self.assertEqual(self.store.state_of("d1"), State.DONE)  # состояние не испорчено

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

    # СТ-30: in_flight — событие вне терминала
    def test_in_flight(self):
        e = make_event()
        self.store.record_received(e)
        self.assertTrue(self.store.in_flight(e.business_key))
        self.store.transition("d1", State.QUEUED)
        self.store.transition("d1", State.PROCESSING)
        self.store.transition("d1", State.DONE)
        self.assertFalse(self.store.in_flight(e.business_key))  # терминал — не в работе

    # СТ-32: счётчик reconcile-циклов
    def test_reconcile_counter(self):
        bk = "o/r#7@abc:/review"
        self.assertEqual(self.store.reconcile_cycles(bk), 0)
        self.assertEqual(self.store.bump_reconcile(bk), 1)
        self.assertEqual(self.store.bump_reconcile(bk), 2)
        self.store.clear_reconcile(bk)
        self.assertEqual(self.store.reconcile_cycles(bk), 0)

    def test_stale_excludes_terminal(self):
        self.store.record_received(make_event())
        self.store.transition("d1", State.QUEUED)
        self.store.transition("d1", State.PROCESSING)
        self.store.transition("d1", State.DONE)
        self.clock.t += 100
        self.assertEqual(self.store.stale(deadline_seconds=60), [])


if __name__ == "__main__":
    unittest.main()
