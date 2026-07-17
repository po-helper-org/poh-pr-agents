"""СТ-14..16: обработка одного события (одна попытка, без ретрая/эскалации)."""
import unittest

from reliability.state import Backpressure, Event, State, StateStore
from reliability.supervisor import process


class FakeAnalyze:
    def __init__(self, exc=None):
        self.exc, self.calls = exc, 0

    def __call__(self, event):
        self.calls += 1
        if self.exc:
            raise self.exc


def ev(delivery="d1", head="abc", command="/review"):
    return Event(delivery_id=delivery, repo="o/r", number=7, head_sha=head, command=command)


class TestProcess(unittest.TestCase):
    def setUp(self):
        self.store = StateStore(":memory:")

    def test_success_marks_done(self):
        e = ev()
        self.store.record_received(e)
        a = FakeAnalyze()
        r = process(e, a, self.store)
        self.assertEqual(r.state, State.DONE)
        self.assertEqual(a.calls, 1)

    def test_failure_marks_failed(self):
        e = ev()
        self.store.record_received(e)
        a = FakeAnalyze(exc=TimeoutError("stall"))
        r = process(e, a, self.store)
        self.assertEqual(r.state, State.FAILED)  # эскалация — на воркере, не здесь
        self.assertEqual(r.error, "TimeoutError")  # точный класс — в DLQ-коммент (К-5)

    def test_backpressure_reraised_not_marked_failed(self):
        # rate limit — не сбой: process пробрасывает наверх, НЕ метит FAILED
        e = ev()
        self.store.record_received(e)
        a = FakeAnalyze(exc=Backpressure("rate limited"))
        with self.assertRaises(Backpressure):
            process(e, a, self.store)
        self.assertNotEqual(self.store.state_of(e.delivery_id), State.FAILED)

    def test_base_exception_propagates(self):
        e = ev()
        self.store.record_received(e)
        a = FakeAnalyze(exc=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            process(e, a, self.store)

    def test_already_done_skips_analysis(self):
        a1 = ev(delivery="a:/review")
        self.store.record_received(a1)
        process(a1, FakeAnalyze(), self.store)  # DONE
        b1 = ev(delivery="b:/review")
        self.store.record_received(b1)
        spy = FakeAnalyze()
        r = process(b1, spy, self.store)
        self.assertEqual(r.state, State.DONE)
        self.assertTrue(r.skipped)
        self.assertEqual(spy.calls, 0)  # СТ-16

    def test_redelivered_done_event_is_noop(self):
        # СТ-17: воркер упал после DONE до ack → передоставка того же delivery_id
        e = ev()
        self.store.record_received(e)
        process(e, FakeAnalyze(), self.store)  # DONE
        spy = FakeAnalyze()
        r = process(e, spy, self.store)  # не должно падать IllegalTransition
        self.assertEqual(r.state, State.DONE)
        self.assertTrue(r.skipped)
        self.assertEqual(spy.calls, 0)

    def test_force_bypasses_already_done(self):
        a1 = ev(delivery="a:/review")
        self.store.record_received(a1)
        process(a1, FakeAnalyze(), self.store)  # DONE
        b1 = ev(delivery="b:/review")
        self.store.record_received(b1)
        spy = FakeAnalyze()
        r = process(b1, spy, self.store, force=True)
        self.assertEqual(spy.calls, 1)  # reconcile — не пропускаем
        self.assertEqual(r.state, State.DONE)
        self.assertFalse(r.skipped)

    def test_concurrent_same_business_key_analyzes_once(self):
        # СТ-16: две доставки одного бизнес-ключа приходят конкурентно (разные
        # delivery_id). Симулируем гонку: пока e1 в анализе (in-flight, захват
        # держится), реентрантно приходит e2. e2 обязан пропустить анализ.
        e1 = ev(delivery="a:/review")
        e2 = ev(delivery="b:/review")  # тот же business_key
        self.assertEqual(e1.business_key, e2.business_key)
        self.store.record_received(e1)
        self.store.record_received(e2)
        spy2 = FakeAnalyze()
        captured = {}

        class Reentrant:
            def __init__(s):
                s.calls = 0

            def __call__(s, event):
                s.calls += 1
                captured["r2"] = process(e2, spy2, self.store)  # e2 «конкурентно»

        r1 = process(e1, Reentrant(), self.store)
        self.assertEqual(r1.state, State.DONE)
        self.assertEqual(spy2.calls, 0)              # e2 увидел захват → анализ не запускал
        self.assertTrue(captured["r2"].skipped)

    def test_claim_released_on_failure_allows_retry(self):
        # Сбой освобождает захват → повторная попытка того же delivery_id проходит.
        e = ev()
        self.store.record_received(e)
        process(e, FakeAnalyze(exc=TimeoutError("boom")), self.store)  # FAILED, release
        self.assertIsNone(self.store.claim_holder(e.business_key))
        spy = FakeAnalyze()
        r = process(e, spy, self.store)              # ретрай
        self.assertEqual(r.state, State.DONE)
        self.assertEqual(spy.calls, 1)

    def test_claim_released_on_success(self):
        e = ev()
        self.store.record_received(e)
        process(e, FakeAnalyze(), self.store)        # DONE
        self.assertIsNone(self.store.claim_holder(e.business_key))

    def test_force_respects_active_claim(self):
        # reconcile (force) не должен дублировать анализ поверх активного in-flight.
        e1 = ev(delivery="a:/review")
        e2 = ev(delivery="b:/review")  # тот же business_key
        self.store.record_received(e1)
        self.store.record_received(e2)
        spy2 = FakeAnalyze()
        captured = {}

        class Reentrant:
            def __call__(s, event):
                captured["r2"] = process(e2, spy2, self.store, force=True)

        process(e1, Reentrant(), self.store)
        self.assertEqual(spy2.calls, 0)              # захват держится → force пропускает
        self.assertTrue(captured["r2"].skipped)


if __name__ == "__main__":
    unittest.main()
