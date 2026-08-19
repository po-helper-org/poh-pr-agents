"""Worker (СТ-14..18): lease → process с per-task таймаутом → ack/nack.

Ретрай и DLQ — на очереди. Успех → ack; сбой/таймаут → nack (очередь повторит
с backoff или уведёт в dead-letter по исчерпании выдач). При dead-letter воркер
доводит событие до терминала, постит видимый коммент в PR (СТ-27) и метрику.
Механизм таймаута инъектируется (`run_fn`) → логика тестируется без потоков/времени.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Callable, Optional

from reliability import agent_event, metrics, sentry_setup
from reliability.mapreduce import CHUNK_EVENT, REDUCE_EVENT
from reliability.notifier import GitHubClient, notify_failure
from reliability.queue import DurableQueue, Lease
from reliability.state import Backpressure, State, StateStore, event_from_dict
from reliability.supervisor import process

_MAPREDUCE_EVENTS = frozenset({CHUNK_EVENT, REDUCE_EVENT})

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


def _report_to_issue_cycle(client, event, status: str, detail: str = "") -> None:
    """Доложить циклу Issue о состоянии ревью (H3).

    Только `/review`: `/describe` переписывает описание PR и фазу задачи не
    двигает — доложить о нём как о ревью значило бы сдвинуть фазу работой,
    которой не было.

    Ключ задачи берётся из тела PR (`Closes #N`). Не нашёлся — событие всё
    равно уходит: Issue-Agent запишет его как сироту, и факт останется видимым,
    вместо того чтобы исчезнуть здесь.

    Ничего не поднимает: ревью уже опубликовано, и сбой доклада не должен
    превращать сделанную работу в nack.
    """
    if event.command != "/review" or not agent_event.configured():
        return
    try:
        root = agent_event.parse_root_issue(client.get_pull_body(event.repo, event.number))
        agent_event.report(event.repo, event.number, status,
                           root_issue=root, detail=detail)
    except Exception as exc:
        logger.warning("доклад в цикл Issue не собран (%s#%s): %s",
                       event.repo, event.number, exc)


def handle_lease(lease: Lease, *, queue: DurableQueue, store: StateStore,
                 client: GitHubClient, analyze, run_fn=run_with_timeout,
                 task_timeout: float = 90, max_attempts: int = 5, backoff: float = 0,
                 backpressure_delay: float = 5.0, mapreduce_handle=None) -> str:
    # map-reduce события (chunk/reduce) — отдельный durable-путь, мимо state-machine
    # обычных событий (их координация в job-сторе). Включается только при активном
    # флаге (mapreduce_handle задан); иначе таких событий в очереди не бывает.
    if mapreduce_handle is not None and lease.payload.get("event_type") in _MAPREDUCE_EVENTS:
        return mapreduce_handle(lease, queue=queue, store=store, client=client,
                                max_attempts=max_attempts, backoff=backoff,
                                backpressure_delay=backpressure_delay)
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
            # Доклад только по фактически выполненной работе: `skipped` означает,
            # что ревью сделал сиблинг, и он же о нём доложил. Второй доклад тем
            # же ключом цикл схлопнет, но слать его — притворяться, что мы
            # что-то сделали.
            if not result.skipped:
                _report_to_issue_cycle(client, event, agent_event.STARTED)
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
        sentry_setup.capture_dead_letter(event, reason or "unknown", lease.attempts)
        notify_failure(client, event, reason, lease.attempts, escalated=True)  # точный класс сбоя
        _report_to_issue_cycle(client, event, agent_event.FAILED,
                               detail=f"ревью не выполнено: {reason}")
        logger.warning("processed: delivery=%s command=%s → DEAD-LETTER (reason=%s attempts=%d) "
                       "— видимый коммент в PR", event.delivery_id, event.command, reason, lease.attempts)
    else:
        logger.info("processed: delivery=%s command=%s → %s (reason=%s attempts=%d)",
                    event.delivery_id, event.command, outcome, reason, lease.attempts)
    return outcome


def run_once(queue: DurableQueue, *, store: StateStore, client: GitHubClient, analyze,
             visibility_timeout: float = 120, task_timeout: float = 90,
             max_attempts: int = 5, backoff: float = 0,
             backpressure_delay: float = 5.0, mapreduce_handle=None) -> bool:
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
                 backpressure_delay=backpressure_delay, mapreduce_handle=mapreduce_handle)
    return True


def run_forever(queue, *, store, client, analyze, idle_sleep=1.0, **kw):  # pragma: no cover
    while True:
        if not run_once(queue, store=store, client=client, analyze=analyze, **kw):
            time.sleep(idle_sleep)


def resolve_timeouts(env=None) -> tuple[float, float, float, float]:
    """Читает 4 слоя таймаута из окружения и проверяет инвариант, fail-fast.

    Инвариант: CONFIG_AI_TIMEOUT ≤ ATTEMPT < TASK < VISIBILITY. Один override
    в устаревшем .env (напр. CONFIG_AI_TIMEOUT=600 при TASK=90) молча ломал бы
    порядок и возвращал баг дубль-review — поэтому падаем на старте с явным
    сообщением, а не деградируем в рантайме. Возвращает (ai, attempt, task,
    visibility)."""
    env = env if env is not None else os.environ
    ai = float(env.get("CONFIG_AI_TIMEOUT", "600"))
    attempt = float(env.get("RELIABILITY_ATTEMPT_TIMEOUT", "630"))
    task = float(env.get("RELIABILITY_TASK_TIMEOUT", "660"))
    visibility = float(env.get("RELIABILITY_VISIBILITY_TIMEOUT", "720"))
    if not (ai <= attempt < task < visibility):
        raise ValueError(
            "нарушен инвариант таймаутов: требуется CONFIG_AI_TIMEOUT ≤ "
            "RELIABILITY_ATTEMPT_TIMEOUT < RELIABILITY_TASK_TIMEOUT < "
            "RELIABILITY_VISIBILITY_TIMEOUT, получено "
            f"ai={ai} attempt={attempt} task={task} visibility={visibility}")
    return ai, attempt, task, visibility


def main():  # pragma: no cover - deploy entrypoint (отдельный процесс воркера)
    from reliability import analyze_adapter, logging_setup, sentry_setup
    logging_setup.configure()  # reliability.* → stdout (логи обработки в контейнере worker)
    sentry_setup.configure("worker")  # no-op без SENTRY_DSN
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
    # Слои таймаута (все в секундах, ↑ управляются через .env). LLM-кап = 10 мин
    # (CONFIG_AI_TIMEOUT=600); внешние гварды с запасом, чтобы НЕ прервать
    # легитимно идущее 10-мин ревью и не передоставить его в очередь (иначе
    # конкурентный дубль-review). resolve_timeouts проверяет инвариант fail-fast.
    _ai_timeout, attempt_timeout, task_timeout, visibility_timeout = resolve_timeouts()

    rate = float(os.environ.get("RELIABILITY_LLM_RPS", "3"))
    burst = float(os.environ.get("RELIABILITY_LLM_BURST", "6"))
    gateway = Gateway(
        [Provider("zai", analyze_adapter.run,
                  breaker=CircuitBreaker(
                      failure_threshold=int(os.environ.get("RELIABILITY_CB_THRESHOLD", "5")),
                      reset_timeout=float(os.environ.get("RELIABILITY_CB_RESET", "30"))))],
        limiter=TokenBucket(rate=rate, capacity=burst),
        attempt_timeout=attempt_timeout)

    # ── map-reduce (ФТ-APRP-2/6/8, пункт B) — только при флаге RELIABILITY_MAPREDUCE ──
    # OFF по умолчанию: прод-путь одиночного прохода не меняется, мерж в main безопасен.
    analyze = gateway.run
    mapreduce_handle = None
    if os.environ.get("RELIABILITY_MAPREDUCE", "").strip().lower() in ("1", "true", "yes", "on"):
        from reliability import chunk_review, mapreduce_worker
        deep_review = lambda fwp: chunk_review.review_chunk(chunk_review.glm_model_call, fwp)

        def mapreduce_handle(lease, **kw):  # noqa: E731 - тонкая привязка review
            return mapreduce_worker.handle(lease, review=deep_review, **kw)

        chunk_budget = int(os.environ.get("RELIABILITY_CHUNK_BUDGET_TOKENS", "12000"))
        total_budget = int(os.environ.get("RELIABILITY_TOTAL_BUDGET_TOKENS", "0"))
        _base_analyze = gateway.run

        def analyze(event):  # маршрутизация большого /review в fan-out
            if event.event_type == "pull_request" and event.command == "/review" \
                    and mapreduce_worker.route_and_fanout(
                        event, client=client, store=store, queue=queue,
                        list_files=client.list_pull_files,
                        chunk_budget_tokens=chunk_budget, total_budget_tokens=total_budget):
                return
            return _base_analyze(event)
        logger.info("map-reduce ВКЛЮЧЁН (RELIABILITY_MAPREDUCE): большой /review идёт по частям")

    run_forever(queue, store=store, client=client, analyze=analyze,
                task_timeout=task_timeout,
                visibility_timeout=visibility_timeout,
                max_attempts=int(os.environ.get("RELIABILITY_MAX_ATTEMPTS", "5")),
                backoff=float(os.environ.get("RELIABILITY_BACKOFF", "10")),          # ×attempts на сбое
                backpressure_delay=float(os.environ.get("RELIABILITY_BACKPRESSURE_DELAY", "5")),
                mapreduce_handle=mapreduce_handle)


if __name__ == "__main__":  # pragma: no cover
    main()
