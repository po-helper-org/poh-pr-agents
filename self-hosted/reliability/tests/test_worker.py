"""СТ-14..18: worker — lease→process→ack/nack, retry/DLQ на очереди, коммент при DLQ."""
import unittest

from reliability import metrics
from reliability.gateway import GatewayCircuitOpen
from reliability.queue import DurableQueue
from reliability.state import Backpressure, Event, State, StateStore, event_to_dict
from reliability.supervisor import process
from reliability.worker import TaskTimeout, handle_lease, resolve_timeouts, run_once


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

    def test_gateway_circuit_open_deferred_not_dead_lettered(self):
        # Регрессия каскада: org-wide бэклог + аутейдж Z.AI размыкает circuit →
        # GatewayCircuitOpen. Событие должно ОТЛОЖИТЬСЯ (как backpressure), а не
        # выжечь max_attempts и залить весь бэклог провал-комментами. «Не молчать»
        # на затяжной простой — забота эскалации свипера, не воркера.
        self._enqueue()
        circuit_open = FakeAnalyze(exc=GatewayCircuitOpen("all circuits open"))
        out = self._handle(circuit_open, max_attempts=1)   # порог=1 — поймали бы ложный DLQ
        self.assertEqual(out, "deferred")
        self.assertEqual(self.client.calls, [])            # НЕТ провал-коммента
        self.assertEqual(metrics.get("dead_letter_total"), 0)
        self.assertEqual(len(self.queue.dead_letters()), 0)
        self.assertEqual(metrics.get("backpressure_deferred"), 1)
        self.assertNotEqual(self.store.state_of("d1"), State.FAILED)  # не сбой попытки
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


class ResolveTimeoutsTest(unittest.TestCase):
    def test_defaults_hold_invariant(self):
        ai, attempt, task, visibility = resolve_timeouts({})
        self.assertEqual((ai, attempt, task, visibility), (600.0, 630.0, 660.0, 720.0))
        self.assertLessEqual(ai, attempt)
        self.assertLess(attempt, task)
        self.assertLess(task, visibility)

    def test_custom_valid_override(self):
        env = {"CONFIG_AI_TIMEOUT": "300", "RELIABILITY_ATTEMPT_TIMEOUT": "300",
               "RELIABILITY_TASK_TIMEOUT": "330", "RELIABILITY_VISIBILITY_TIMEOUT": "360"}
        self.assertEqual(resolve_timeouts(env), (300.0, 300.0, 330.0, 360.0))

    def test_stale_env_breaks_invariant_fails_fast(self):
        # CONFIG_AI_TIMEOUT=600, но устаревший TASK=90 → падаем на старте.
        env = {"CONFIG_AI_TIMEOUT": "600", "RELIABILITY_TASK_TIMEOUT": "90"}
        with self.assertRaises(ValueError):
            resolve_timeouts(env)

    def test_task_not_below_visibility(self):
        env = {"RELIABILITY_TASK_TIMEOUT": "800", "RELIABILITY_VISIBILITY_TIMEOUT": "720"}
        with self.assertRaises(ValueError):
            resolve_timeouts(env)


if __name__ == "__main__":
    unittest.main()
