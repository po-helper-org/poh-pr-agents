"""Пункт B: активация map-reduce — маршрутизация fan-out + durable chunk/reduce."""
import unittest

from reliability import ack
from reliability.mapreduce import REDUCE_EVENT, job_key_for
from reliability.mapreduce_worker import handle, pr_partition, route_and_fanout
from reliability.queue import DurableQueue
from reliability.reduce import REVIEW_MARKER
from reliability.state import Backpressure, Event, StateStore


class FakeClient:
    def __init__(self, files):
        self.files = files
        self.comments = {}

    def list_pull_files(self, repo, number):
        return self.files

    def upsert_comment(self, repo, number, marker, body):
        self.comments[marker] = body


def ev(cmd="/review", etype="pull_request"):
    return Event("d1", "o/r", 7, "abc", cmd, etype)


def _files(n, adds=200):
    return [{"filename": f"f{i}.py", "additions": adds, "deletions": 0,
             "status": "modified", "patch": f"@@\n+code{i}"} for i in range(n)]


class TestRouteAndFanout(unittest.TestCase):
    def test_small_returns_false_no_side_effects(self):
        client = FakeClient(_files(1, adds=5))
        store, q = StateStore(":memory:"), DurableQueue(":memory:")
        out = route_and_fanout(ev(), client=client, store=store, queue=q,
                               list_files=client.list_pull_files, chunk_budget_tokens=10_000)
        self.assertFalse(out)
        self.assertEqual(q.depth(), 0)
        self.assertEqual(client.comments, {})

    def test_large_fans_out(self):
        client = FakeClient(_files(6))
        store, q = StateStore(":memory:"), DurableQueue(":memory:")
        out = route_and_fanout(ev(), client=client, store=store, queue=q,
                               list_files=client.list_pull_files, chunk_budget_tokens=3_000)
        self.assertTrue(out)
        self.assertIn(ack.ACK_MARKER, client.comments)          # fast-ack
        self.assertGreater(q.depth(), 0)                        # чанки в очереди
        self.assertIsNotNone(store.job_status(job_key_for("o/r", 7, "abc")))

    def test_idempotent_no_double_fanout(self):
        client = FakeClient(_files(6))
        store, q = StateStore(":memory:"), DurableQueue(":memory:")
        route_and_fanout(ev(), client=client, store=store, queue=q,
                         list_files=client.list_pull_files, chunk_budget_tokens=3_000)
        d1 = q.depth()
        route_and_fanout(ev(), client=client, store=store, queue=q,       # повтор доставки
                         list_files=client.list_pull_files, chunk_budget_tokens=3_000)
        self.assertEqual(q.depth(), d1)                          # job уже есть → без дублей


class TestHandleChunk(unittest.TestCase):
    def _setup(self, total, files=None):
        store, q = StateStore(":memory:"), DurableQueue(":memory:")
        jk = job_key_for("o/r", 7, "abc")
        store.create_job(jk, "abc", total)
        client = FakeClient(files or [{"filename": "a.py", "patch": "pa"}])
        return store, q, jk, client

    def _enqueue_chunk(self, q, jk, idx, files):
        q.enqueue({"event_type": "chunk", "repo": "o/r", "number": 7, "head_sha": "abc",
                   "job_key": jk, "chunk_index": idx, "files": files}, pr_partition("o/r", 7))

    def test_chunk_ok_records_progress_no_early_reduce(self):
        store, q, jk, client = self._setup(2)
        self._enqueue_chunk(q, jk, 0, ["a.py"])
        lease = q.lease(visibility_timeout=30)
        out = handle(lease, queue=q, store=store, client=client, review=lambda fwp: "нашёл баг")
        self.assertEqual(out, "ack")
        self.assertEqual(store.job_status(jk)["done_chunks"], 1)
        self.assertIn(ack.ACK_MARKER, client.comments)          # прогресс
        self.assertEqual(q.depth(), 0)                          # 1/2 → reduce ещё нет

    def test_last_chunk_enqueues_reduce(self):
        store, q, jk, client = self._setup(1)
        self._enqueue_chunk(q, jk, 0, ["a.py"])
        lease = q.lease(visibility_timeout=30)
        handle(lease, queue=q, store=store, client=client, review=lambda fwp: "ок")
        self.assertEqual(q.depth(), 1)                          # reduce поставлен
        rl = q.lease(visibility_timeout=30)
        self.assertEqual(rl.payload["event_type"], REDUCE_EVENT)

    def test_chunk_failure_dead_letter_records_failed_and_reduces(self):
        store, q, jk, client = self._setup(1)
        self._enqueue_chunk(q, jk, 0, ["a.py"])
        lease = q.lease(visibility_timeout=30, max_attempts=1)   # одна выдача

        def boom(fwp):
            raise RuntimeError("z.ai timeout")

        out = handle(lease, queue=q, store=store, client=client, review=boom, max_attempts=1)
        self.assertEqual(out, "dead_letter")
        self.assertEqual(store.job_status(jk)["failed_chunks"], 1)   # partial, не молчим
        self.assertEqual(q.depth(), 1)                              # всё отчиталось → reduce

    def test_chunk_backpressure_defers(self):
        store, q, jk, client = self._setup(1)
        self._enqueue_chunk(q, jk, 0, ["a.py"])
        lease = q.lease(visibility_timeout=30)

        def rl(fwp):
            raise Backpressure("rate limit")

        out = handle(lease, queue=q, store=store, client=client, review=rl)
        self.assertEqual(out, "deferred")
        self.assertEqual(store.job_status(jk)["done_chunks"], 0)    # не засчитан


class TestHandleReduce(unittest.TestCase):
    def test_publishes_single_review(self):
        store, q = StateStore(":memory:"), DurableQueue(":memory:")
        jk = job_key_for("o/r", 7, "abc")
        store.create_job(jk, "abc", 2)
        store.record_chunk_finding(jk, 0, ["a.py"], "замечание A", True)
        store.record_chunk_finding(jk, 1, ["b.py"], "", False)      # partial
        client = FakeClient([])
        q.enqueue({"event_type": REDUCE_EVENT, "repo": "o/r", "number": 7, "job_key": jk},
                  pr_partition("o/r", 7))
        lease = q.lease(visibility_timeout=30)
        out = handle(lease, queue=q, store=store, client=client, review=lambda f: "")
        self.assertEqual(out, "ack")
        self.assertIn(REVIEW_MARKER, client.comments)
        self.assertIn("Не отревьюено", client.comments[REVIEW_MARKER])  # b.py помечен


if __name__ == "__main__":
    unittest.main()
