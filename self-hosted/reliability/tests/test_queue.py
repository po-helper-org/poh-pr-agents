"""СТ-6..9: durable queue — at-least-once, visibility-timeout, DLQ, честность."""
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
        self.assertIsNotNone(lease)
        self.assertEqual(lease.payload["cmd"], "/review")
        self.assertEqual(lease.partition, "o/r")
        self.assertEqual(lease.attempts, 1)

    def test_lease_empty_returns_none(self):
        self.assertIsNone(self.q.lease(visibility_timeout=30))

    def test_ack_removes_message(self):
        self.q.enqueue({"x": 1}, "o/r")
        lease = self.q.lease(visibility_timeout=30)
        self.q.ack(lease.id)
        self.assertEqual(self.q.depth(), 0)
        self.assertIsNone(self.q.lease(visibility_timeout=30))

    def test_leased_message_hidden_until_visibility_timeout(self):
        self.q.enqueue({"x": 1}, "o/r")
        first = self.q.lease(visibility_timeout=30)
        # ещё арендовано → не выдаётся
        self.assertIsNone(self.q.lease(visibility_timeout=30))
        # после истечения visibility-timeout → redelivery (СТ-6/17)
        self.clock.t += 31
        again = self.q.lease(visibility_timeout=30)
        self.assertEqual(again.id, first.id)
        self.assertEqual(again.attempts, 2)  # повторная выдача считается попыткой

    def test_nack_requeues_for_retry(self):
        self.q.enqueue({"x": 1}, "o/r")
        lease = self.q.lease(visibility_timeout=30)
        self.assertEqual(self.q.nack(lease.id, max_attempts=3), "requeued")
        again = self.q.lease(visibility_timeout=30)
        self.assertEqual(again.id, lease.id)
        self.assertEqual(again.attempts, 2)

    def test_nack_backoff_delays_availability(self):
        self.q.enqueue({"x": 1}, "o/r")
        lease = self.q.lease(visibility_timeout=30)
        self.q.nack(lease.id, max_attempts=3, backoff=100)
        self.assertIsNone(self.q.lease(visibility_timeout=30))  # ещё в backoff
        self.clock.t += 101
        self.assertIsNotNone(self.q.lease(visibility_timeout=30))

    def test_dead_letter_after_max_attempts(self):
        self.q.enqueue({"x": 1}, "o/r")
        for _ in range(2):
            lease = self.q.lease(visibility_timeout=30)
            self.assertEqual(self.q.nack(lease.id, max_attempts=3), "requeued")
        lease = self.q.lease(visibility_timeout=30)  # attempts=3
        self.assertEqual(self.q.nack(lease.id, max_attempts=3, reason="boom"), "dead_letter")
        self.assertEqual(self.q.depth(), 0)
        dl = self.q.dead_letters()
        self.assertEqual(len(dl), 1)
        self.assertEqual(dl[0]["reason"], "boom")

    def test_partition_fairness_interleaves(self):
        # СТ-7: один «тяжёлый» репо не голодит остальные
        self.q.enqueue({"n": 1}, "A")
        self.q.enqueue({"n": 2}, "A")
        self.q.enqueue({"n": 3}, "B")
        order = [self.q.lease(visibility_timeout=30).partition for _ in range(3)]
        self.assertEqual(order, ["A", "B", "A"])  # B не в конце


if __name__ == "__main__":
    unittest.main()
