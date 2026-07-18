"""Worker (СТ-14..18): lease → process с per-task таймаутом → ack/nack.

Ретрай и DLQ — на очереди. Успех → ack; сбой/таймаут → nack (очередь повторит
с backoff или уведёт в dead-letter по исчерпании выдач). При dead-letter воркер
доводит событие до терминала, постит видимый коммент в PR (СТ-27) и метрику.
Механизм таймаута инъектируется (`run_fn`) → логика тестируется без потоков/времени.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from reliability import metrics
from reliability.notifier import GitHubClient, notify_failure
from reliability.queue import DurableQueue, Lease
from reliability.state import Backpressure, State, StateStore, event_from_dict
from reliability.supervisor import process

logger = logging.getLogger(__name__)  # reliability.worker → stdout (см. logging_setup)


class TaskTimeout(Exception):
    """Обработка превысила per-task таймаут (СТ-14)."""


def run_with_timeout(fn: Callable, timeout: float):  # pragma: no cover - реальные потоки
    import threading

    box: dict = {}

    def target():
        try:
            box["v"] = fn()
        except BaseException as e:  # noqa: BLE001 — пробрасываем через box
            box["e"] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TaskTimeout("task exceeded timeout")  # осиротевший поток завершится сам
    if "e" in box:
        raise box["e"]
    return box.get("v")


def _drive_to_dead_letter(store: StateStore, delivery_id: str) -> None:
    cur = store.state_of(delivery_id)
    if cur in (State.DONE, State.DEAD_LETTER, None):
        return
    if cur != State.FAILED:
        store.transition(delivery_id, State.FAILED)
    store.transition(delivery_id, State.DEAD_LETTER)


def handle_lease(lease: Lease, *, queue: DurableQueue, store: StateStore,
                 client: GitHubClient, analyze, run_fn=run_with_timeout,
                 task_timeout: float = 90, max_attempts: int = 5, backoff: float = 0,
                 backpressure_delay: float = 5.0) -> str:
    event = event_from_dict(lease.payload)
    force = event.event_type == "reconcile"
    reason: Optional[str] = None
    try:
        result = run_fn(lambda: process(event, analyze, store, force=force), task_timeout)
        # skipped=True — работа уже сделана/захвачена сиблингом: ack, не nack
        # (иначе проигравший в гонке за бизнес-ключ копит attempts → ложный DLQ).
        if result.state == State.DONE or result.skipped:
            queue.ack(lease.id, lease.token)
            metrics.incr("processed_ok")
            logger.info("processed: delivery=%s command=%s → ack%s",
                        event.delivery_id, event.command,
                        " (skipped: already done/in-flight)" if result.skipped else "")
            return "ack"
        reason = result.error or "analysis_failed"   # точный класс сбоя → в коммент/метрику
    except Backpressure:
        # локальный rate limit — НЕ сбой: откладываем без счёта к DLQ и без коммента
        # (иначе троттлинг штампует ложные провалы и воркер спинит вхолостую).
        queue.defer(lease.id, lease.token, delay=backpressure_delay)
        metrics.incr("backpressure_deferred")
        logger.info("processed: delivery=%s command=%s → deferred (rate limit, %ss)",
                    event.delivery_id, event.command, backpressure_delay)
        return "deferred"
    except Exception as err:  # таймаут или неожиданная ошибка обработки
        reason = type(err).__name__

    # backoff растёт с числом выдач — не долбим мёртвый Z.AI и не спиним вхолостую
    effective_backoff = backoff * lease.attempts if backoff else 0
    outcome = queue.nack(lease.id, lease.token, max_attempts=max_attempts,
                         backoff=effective_backoff, reason=reason)
    if outcome == "dead_letter":  # исчерпаны выдачи → эскалация (СТ-27)
        _drive_to_dead_letter(store, event.delivery_id)
        # Освобождаем захват бизнес-ключа: process() мог быть брошен по таймауту и
        # не вызвать release_claim → иначе захват утёк бы навсегда и заблокировал
        # reconcile-восстановление (К-1). try_claim самозалечивается и без этого,
        # но снимаем сразу, не заставляя сиблинга ждать своей следующей попытки.
        store.release_claim(event.business_key, event.delivery_id)
        metrics.incr("dead_letter_total")
        notify_failure(client, event, reason, lease.attempts, escalated=True)  # точный класс сбоя
        logger.warning("processed: delivery=%s command=%s → DEAD-LETTER (reason=%s attempts=%d) "
                       "— видимый коммент в PR", event.delivery_id, event.command, reason, lease.attempts)
    else:
        logger.info("processed: delivery=%s command=%s → %s (reason=%s attempts=%d)",
                    event.delivery_id, event.command, outcome, reason, lease.attempts)
    return outcome


def run_once(queue: DurableQueue, *, store: StateStore, client: GitHubClient, analyze,
             visibility_timeout: float = 120, task_timeout: float = 90,
             max_attempts: int = 5, backoff: float = 0,
             backpressure_delay: float = 5.0) -> bool:
    """Обработать одно сообщение; False если очередь пуста.

    Инвариант: visibility_timeout > task_timeout. Воркер бросает задачу по
    task_timeout (90с) раньше, чем очередь передоставит по visibility_timeout
    (120с) — иначе одно и то же сообщение могло бы обрабатываться дважды
    конкурентно (re-entrant-захват их пропустил бы). Дубль всё равно
    идемпотентен через upsert (СТ-25), но инвариант исключает саму гонку.
    """
    lease = queue.lease(visibility_timeout=visibility_timeout, max_attempts=max_attempts)
    if lease is None:
        return False
    handle_lease(lease, queue=queue, store=store, client=client, analyze=analyze,
                 task_timeout=task_timeout, max_attempts=max_attempts, backoff=backoff,
                 backpressure_delay=backpressure_delay)
    return True


def run_forever(queue, *, store, client, analyze, idle_sleep=1.0, **kw):  # pragma: no cover
    while True:
        if not run_once(queue, store=store, client=client, analyze=analyze, **kw):
            time.sleep(idle_sleep)


def main():  # pragma: no cover - deploy entrypoint (отдельный процесс воркера)
    import os

    from reliability import analyze_adapter, logging_setup
    logging_setup.configure()  # reliability.* → stdout (логи обработки в контейнере worker)
    from reliability.gateway import CircuitBreaker, Gateway, Provider, TokenBucket
    from reliability.github_client import GitHubAppClient

    store = StateStore(os.environ.get("RELIABILITY_DB", "/data/reliability.db"))
    queue = DurableQueue(os.environ.get("RELIABILITY_QUEUE", "/data/queue.db"))
    client = GitHubAppClient(token_provider=analyze_adapter.installation_token)

    # LLM Gateway: один провайдер Z.AI (через pr-agent). Circuit breaker гасит
    # штормовые ретраи при аутейдже Z.AI (быстрый видимый отказ, не тишина, К-1),
    # rate limit держит поток под лимитом. Добавить ключ/провайдера — расширить
    # список Provider(...). Таймаут попытки < worker task_timeout, чтобы сбой
    # засчитался цепи внутри gateway, а не съелся внешним таймаутом.
    # ⚠️ rate limit ПРОЦЕССНЫЙ: при N воркерах суммарный RPS ≈ N×rate. Задавать
    # RELIABILITY_LLM_RPS ≈ (лимит Z.AI) / (макс. число реплик воркера).
    rate = float(os.environ.get("RELIABILITY_LLM_RPS", "3"))
    burst = float(os.environ.get("RELIABILITY_LLM_BURST", "6"))
    gateway = Gateway(
        [Provider("zai", analyze_adapter.run,
                  breaker=CircuitBreaker(
                      failure_threshold=int(os.environ.get("RELIABILITY_CB_THRESHOLD", "5")),
                      reset_timeout=float(os.environ.get("RELIABILITY_CB_RESET", "30"))))],
        limiter=TokenBucket(rate=rate, capacity=burst),
        attempt_timeout=float(os.environ.get("RELIABILITY_ATTEMPT_TIMEOUT", "75")))

    run_forever(queue, store=store, client=client, analyze=gateway.run,
                task_timeout=float(os.environ.get("RELIABILITY_TASK_TIMEOUT", "90")),
                max_attempts=int(os.environ.get("RELIABILITY_MAX_ATTEMPTS", "5")),
                backoff=float(os.environ.get("RELIABILITY_BACKOFF", "10")),          # ×attempts на сбое
                backpressure_delay=float(os.environ.get("RELIABILITY_BACKPRESSURE_DELAY", "5")))


if __name__ == "__main__":  # pragma: no cover
    main()
