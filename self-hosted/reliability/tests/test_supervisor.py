"""СТ-14..16: обработка одного события (одна попытка, без ретрая/эскалации)."""
import unittest

from reliability.state import Event, State, StateStore
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


if __name__ == "__main__":
    unittest.main()
