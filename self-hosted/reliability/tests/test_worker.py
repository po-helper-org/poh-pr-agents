"""СТ-14..18: worker — lease→process→ack/nack, retry/DLQ на очереди, коммент при DLQ."""
import unittest

from reliability import metrics
from reliability.queue import DurableQueue
from reliability.state import Backpressure, Event, State, StateStore, event_to_dict
from reliability.supervisor import process
from reliability.worker import (
    TaskTimeout,
    handle_lease,
    resolve_worker_timeouts,
    run_once,
)


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

    def upsert_comment(self, repo, number, marker, body):
        self.calls.append((repo, number, marker, body))


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

    def test_redelivered_done_event_acks_without_false_comment(self):
        # СТ-17 chaos: успешно завершённое событие передоставлено → ack, без ложной эскалации
        self._enqueue()
        self._handle(FakeAnalyze())  # → DONE + ack
        e = Event("d1", "o/r", 7, "abc", "/review")
        self.queue.enqueue(event_to_dict(e), e.repo)  # передоставка того же delivery
        out = self._handle(FakeAnalyze())
        self.assertEqual(out, "ack")
        self.assertEqual(self.client.calls, [])              # НЕТ ложного коммента
        self.assertEqual(metrics.get("dead_letter_total"), 0)

    def test_claim_loss_acks_without_false_dead_letter(self):
        # СТ-16: доставка, проигравшая захват бизнес-ключа (сиблинг in-flight),
        # должна ack, а не nack — иначе копит attempts → ложный DLQ-коммент.
        e = Event("b:/review", "o/r", 7, "abc", "/review")
        self.store.record_received(e)
        self.queue.enqueue(event_to_dict(e), e.repo)
        # сиблинг того же бизнес-ключа держит захват (эмулируем in-flight)
        self.store.try_claim(e.business_key, "a:/review")
        spy = FakeAnalyze()
        out = self._handle(spy, max_attempts=1)          # порог DLQ=1 — поймали бы ложный DLQ
        self.assertEqual(out, "ack")
        self.assertEqual(spy.calls, 0)                   # анализ не запускали
        self.assertEqual(self.client.calls, [])          # НЕТ ложного коммента
        self.assertEqual(metrics.get("dead_letter_total"), 0)

    def test_leaked_claim_after_dead_letter_does_not_block_recovery(self):
        # К-1: анализ завис → таймаут → process брошен, захват держится → e1
        # dead-letter. Захват НЕ должен навсегда блокировать reconcile того же
        # бизнес-ключа. Эмулируем утечку: e1 держит захват и доведён до DEAD_LETTER.
        e1 = Event("e1", "o/r", 7, "abc", "/review")
        self.store.record_received(e1)
        self.store.try_claim(e1.business_key, "e1")       # захват как во время анализа
        for s in (State.QUEUED, State.PROCESSING, State.FAILED, State.DEAD_LETTER):
            self.store.transition("e1", s)
        self.assertEqual(self.store.claim_holder(e1.business_key), "e1")  # утечка
        # reconcile того же бизнес-ключа приходит воркеру — должен восстановить
        self._enqueue(did="reconcile:x", etype="reconcile")
        spy = FakeAnalyze()
        out = self._handle(spy)
        self.assertEqual(out, "ack")
        self.assertEqual(spy.calls, 1)                    # анализ пошёл — К-1 восстановлен

    def test_dead_letter_releases_claim(self):
        # Прямой путь (не таймаут): после dead-letter захват снят сразу.
        self._enqueue()
        e = Event("d1", "o/r", 7, "abc", "/review")
        self._handle(FakeAnalyze(exc=RuntimeError("boom")), max_attempts=1)  # dead_letter
        self.assertEqual(self.store.state_of("d1"), State.DEAD_LETTER)
        self.assertIsNone(self.store.claim_holder(e.business_key))

    def test_rate_limited_healthy_event_deferred_not_dead_lettered(self):
        # HIGH-регрессия: здоровое событие, но лимитер пуст → backpressure. Оно
        # должно ОТЛОЖИТЬСЯ, а не выжечь max_attempts и уйти в ложный DLQ с
        # комментом о провале на PR, где анализ даже не запускался.
        self._enqueue()
        rate_limited = FakeAnalyze(exc=Backpressure("local rate limit"))
        out = self._handle(rate_limited, max_attempts=1)   # порог=1 — поймали бы ложный DLQ
        self.assertEqual(out, "deferred")
        self.assertEqual(self.client.calls, [])            # НЕТ ложного коммента о провале
        self.assertEqual(metrics.get("dead_letter_total"), 0)
        self.assertEqual(len(self.queue.dead_letters()), 0)
        self.assertEqual(metrics.get("backpressure_deferred"), 1)
        self.assertEqual(self.queue.depth(), 1)            # осталось в очереди (отложено)

    def test_dead_letter_comment_carries_real_failure_class(self):
        # К-5: коммент/метрика отражают точный класс сбоя, не generic RuntimeError
        self._enqueue()
        self._handle(FakeAnalyze(exc=ValueError("z.ai 500")), max_attempts=1)
        self.assertEqual(len(self.client.calls), 1)
        _, _, _, body = self.client.calls[0]
        self.assertIn("ValueError", body)                  # реальный класс, не "RuntimeError"

    def test_run_once_empty_returns_false(self):
        self.assertFalse(run_once(self.queue, store=self.store, client=self.client,
                                  analyze=FakeAnalyze()))

    def test_run_once_processes_one(self):
        self._enqueue()
        self.assertTrue(run_once(self.queue, store=self.store, client=self.client,
                                 analyze=FakeAnalyze()))
        self.assertEqual(self.store.state_of("d1"), State.DONE)


class TestResolveWorkerTimeouts(unittest.TestCase):
    """ФТ-APRP-11: вложенность ai < attempt < task < visibility, авто-исправление инверсии."""

    def test_defaults_nested(self):
        # пустой env: attempt/task — дефолты функции; visibility не задан → task+60
        # (явный прод-дефолт 280 задаётся в docker-compose, здесь проверяем fallback кода)
        t = resolve_worker_timeouts({})
        self.assertLess(t["attempt"], t["task"])
        self.assertLess(t["task"], t["visibility"])
        self.assertEqual((t["attempt"], t["task"], t["visibility"]), (200.0, 210.0, 270.0))

    def test_env_overrides_respected(self):
        t = resolve_worker_timeouts({
            "RELIABILITY_ATTEMPT_TIMEOUT": "120",
            "RELIABILITY_TASK_TIMEOUT": "130",
            "RELIABILITY_VISIBILITY_TIMEOUT": "200",
        })
        self.assertEqual((t["attempt"], t["task"], t["visibility"]), (120.0, 130.0, 200.0))

    def test_inverted_task_bumped_above_attempt(self):
        # прежняя инверсия из прода: attempt(75) > task(90)? нет — task<=attempt: 90/75
        t = resolve_worker_timeouts({
            "RELIABILITY_ATTEMPT_TIMEOUT": "90",
            "RELIABILITY_TASK_TIMEOUT": "75",       # <= attempt → авто-подъём
            "RELIABILITY_VISIBILITY_TIMEOUT": "300",
        })
        self.assertGreater(t["task"], t["attempt"])
        self.assertEqual(t["task"], 100.0)          # attempt+10

    def test_visibility_unset_defaults_above_task(self):
        t = resolve_worker_timeouts({
            "RELIABILITY_ATTEMPT_TIMEOUT": "100",
            "RELIABILITY_TASK_TIMEOUT": "110",       # visibility не задан
        })
        self.assertEqual(t["visibility"], 170.0)     # task+60
        self.assertLess(t["task"], t["visibility"])

    def test_visibility_below_task_bumped(self):
        t = resolve_worker_timeouts({
            "RELIABILITY_ATTEMPT_TIMEOUT": "100",
            "RELIABILITY_TASK_TIMEOUT": "150",
            "RELIABILITY_VISIBILITY_TIMEOUT": "120",  # <= task → авто-подъём
        })
        self.assertEqual(t["visibility"], 210.0)      # task+60
        self.assertLess(t["task"], t["visibility"])


class TestMapReduceDispatch(unittest.TestCase):
    """Пункт B: chunk/reduce события уходят в mapreduce_handle, минуя state-machine."""

    def test_chunk_event_dispatched(self):
        store, queue = StateStore(":memory:"), DurableQueue(":memory:")
        queue.enqueue({"event_type": "chunk", "job_key": "j", "repo": "o/r",
                       "number": 7, "chunk_index": 0, "files": ["a.py"]}, "o/r#7")
        lease = queue.lease(visibility_timeout=30)
        seen = {}

        def fake_mr(l, *, queue, store, client, max_attempts, backoff, backpressure_delay):
            seen["type"] = l.payload["event_type"]
            queue.ack(l.id, l.token)
            return "ack"

        out = handle_lease(lease, queue=queue, store=store, client=FakeClient(),
                           analyze=FakeAnalyze(), mapreduce_handle=fake_mr)
        self.assertEqual(out, "ack")
        self.assertEqual(seen["type"], "chunk")    # ушло в диспетчер, не в process()

    def test_normal_event_not_dispatched_to_mapreduce(self):
        store, queue = StateStore(":memory:"), DurableQueue(":memory:")
        e = Event("d1", "o/r", 7, "abc", "/review")
        store.record_received(e)
        queue.enqueue(event_to_dict(e), e.repo)
        lease = queue.lease(visibility_timeout=30)
        called = []
        handle_lease(lease, queue=queue, store=store, client=FakeClient(), analyze=FakeAnalyze(),
                     run_fn=passthrough, mapreduce_handle=lambda *a, **k: called.append(1))
        self.assertEqual(called, [])                # обычное событие — обычный путь
        self.assertEqual(store.state_of("d1"), State.DONE)


if __name__ == "__main__":
    unittest.main()
