"""СТ-6..9: durable queue — at-least-once, visibility-timeout, DLQ, честность, фенсинг."""
import unittest

from reliability.queue import DurableQueue


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class TestDurableQueue(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.q = DurableQueue(":memory:", clock=self.clock)

    def test_enqueue_lease_roundtrip(self):
        self.q.enqueue({"cmd": "/review"}, "o/r")
        lease = self.q.lease(visibility_timeout=30)
        self.assertEqual(lease.payload["cmd"], "/review")
        self.assertEqual(lease.partition, "o/r")
        self.assertEqual(lease.attempts, 1)
        self.assertTrue(lease.token)

    def test_lease_empty_returns_none(self):
        self.assertIsNone(self.q.lease(visibility_timeout=30))

    def test_ack_removes_message(self):
        self.q.enqueue({"x": 1}, "o/r")
        lease = self.q.lease(visibility_timeout=30)
        self.assertTrue(self.q.ack(lease.id, lease.token))
        self.assertEqual(self.q.depth(), 0)
        self.assertIsNone(self.q.lease(visibility_timeout=30))

    def test_defer_delays_and_does_not_count_toward_dlq(self):
        # backpressure: defer возвращает сообщение с задержкой и ОТКАТЫВАЕТ attempts++
        self.q.enqueue({"x": 1}, "o/r")
        lease = self.q.lease(visibility_timeout=30)       # attempts -> 1
        self.assertEqual(lease.attempts, 1)
        self.assertEqual(self.q.defer(lease.id, lease.token, delay=5), "deferred")
        self.assertIsNone(self.q.lease(visibility_timeout=30))  # ещё отложено (delay)
        self.clock.t += 6
        again = self.q.lease(visibility_timeout=30)
        self.assertEqual(again.attempts, 1)               # НЕ 2 — выдача не засчитана
        self.assertEqual(self.q.depth(), 1)               # не в DLQ

    def test_defer_stale_token_noop(self):
        self.q.enqueue({"x": 1}, "o/r")
        lease = self.q.lease(visibility_timeout=30)
        self.assertEqual(self.q.defer(lease.id, "wrong", delay=5), "stale")

    def test_leased_message_hidden_until_visibility_timeout(self):
        self.q.enqueue({"x": 1}, "o/r")
        first = self.q.lease(visibility_timeout=30)
        self.assertIsNone(self.q.lease(visibility_timeout=30))  # ещё арендовано
        self.clock.t += 31
        again = self.q.lease(visibility_timeout=30)  # redelivery (СТ-6/17)
        self.assertEqual(again.id, first.id)
        self.assertEqual(again.attempts, 2)
        self.assertNotEqual(again.token, first.token)  # новый токен аренды

    def test_nack_requeues_for_retry(self):
        self.q.enqueue({"x": 1}, "o/r")
        lease = self.q.lease(visibility_timeout=30)
        self.assertEqual(self.q.nack(lease.id, lease.token, max_attempts=3), "requeued")
        again = self.q.lease(visibility_timeout=30)
        self.assertEqual(again.id, lease.id)
        self.assertEqual(again.attempts, 2)

    def test_nack_backoff_delays_availability(self):
        self.q.enqueue({"x": 1}, "o/r")
        lease = self.q.lease(visibility_timeout=30)
        self.q.nack(lease.id, lease.token, max_attempts=3, backoff=100)
        self.assertIsNone(self.q.lease(visibility_timeout=30))  # ещё в backoff
        self.clock.t += 101
        self.assertIsNotNone(self.q.lease(visibility_timeout=30))

    def test_dead_letter_after_max_attempts_via_nack(self):
        self.q.enqueue({"x": 1}, "o/r")
        for _ in range(2):
            lease = self.q.lease(visibility_timeout=30)
            self.assertEqual(self.q.nack(lease.id, lease.token, max_attempts=3), "requeued")
        lease = self.q.lease(visibility_timeout=30)  # attempts=3
        self.assertEqual(self.q.nack(lease.id, lease.token, max_attempts=3, reason="boom"), "dead_letter")
        self.assertEqual(self.q.depth(), 0)
        dl = self.q.dead_letters()
        self.assertEqual(len(dl), 1)
        self.assertEqual(dl[0]["reason"], "boom")

    def test_dead_letter_via_lease_cap_on_hard_crash(self):
        # СТ-9/К-2: жёсткое падение воркера (нет ни ack, ни nack) не крутит вечно
        self.q.enqueue({"x": 1}, "o/r")
        for _ in range(3):  # три выдачи без ack (краши)
            lease = self.q.lease(visibility_timeout=30, max_attempts=3)
            self.assertIsNotNone(lease)
            self.clock.t += 31  # visibility истёк, воркер не вернулся
        self.assertIsNone(self.q.lease(visibility_timeout=30, max_attempts=3))  # → DLQ
        self.assertEqual(self.q.depth(), 0)
        self.assertEqual(len(self.q.dead_letters()), 1)

    def test_stale_token_ack_is_noop(self):
        self.q.enqueue({"x": 1}, "o/r")
        old = self.q.lease(visibility_timeout=30)
        self.clock.t += 31
        new = self.q.lease(visibility_timeout=30)  # перевыдано другому воркеру
        self.assertFalse(self.q.ack(old.id, old.token))  # опоздавший — no-op
        self.assertEqual(self.q.depth(), 1)              # держит new
        self.assertTrue(self.q.ack(new.id, new.token))
        self.assertEqual(self.q.depth(), 0)

    def test_stale_token_nack_is_noop(self):
        self.q.enqueue({"x": 1}, "o/r")
        old = self.q.lease(visibility_timeout=30)
        self.clock.t += 31
        self.q.lease(visibility_timeout=30)  # перевыдано
        self.assertEqual(self.q.nack(old.id, old.token, max_attempts=3), "stale")

    def test_nack_missing_id(self):
        self.assertEqual(self.q.nack(9999, "tok", max_attempts=3), "missing")

    def test_partition_fairness_interleaves(self):
        # СТ-7: один «тяжёлый» репо не голодит остальные
        self.q.enqueue({"n": 1}, "A")
        self.q.enqueue({"n": 2}, "A")
        self.q.enqueue({"n": 3}, "B")
        order = [self.q.lease(visibility_timeout=30).partition for _ in range(3)]
        self.assertEqual(order, ["A", "B", "A"])  # B не в конце


if __name__ == "__main__":
    unittest.main()
