"""Активация map-reduce в воркере (ФТ-APRP-2/6/8 — пункт B). За фиче-флагом.

Два вклада (оба включаются только при RELIABILITY_MAPREDUCE=1):
1. `route_and_fanout` — маршрутизация большого `/review`: fast-ack + план + fan-out
   событий-чанков; PR-событие завершается планированием (обычный `/describe` и малые
   PR идут прежним одиночным путём — прод-поведение не меняется).
2. `handle` — обработка durable-событий `chunk`/`reduce` (мимо state-machine обычных
   событий; их координация — в job-сторе). Сбой чанка ретраит очередь; при исчерпании
   выдач чанк помечается неуспешным (ok=False) → partial-reduce (не молчим, НФТ-APRP-6).

Чанки/reduce партиционируются ПО PR (`repo#number`) — один большой PR не голодит PR
других (M1, честность). `review`/`list_files` инъектируются → тестируется без сети.
"""
from __future__ import annotations

import logging

from reliability import ack, metrics
from reliability import reduce as reduce_mod
from reliability.chunk_review import patches_for_files
from reliability.mapreduce import (
    CHUNK_EVENT,
    REDUCE_EVENT,
    build_chunk_payloads,
    claim_reduce,
    collect_results,
    job_key_for,
    route,
)
from reliability.sizing import SizeClass, files_from_api
from reliability.state import Backpressure

logger = logging.getLogger(__name__)
EVENT_TYPES = frozenset({CHUNK_EVENT, REDUCE_EVENT})


def pr_partition(repo: str, number: int) -> str:
    """Суб-партиция на PR (M1): чанки одного большого PR не голодят другие PR."""
    return f"{repo}#{number}"


def route_and_fanout(event, *, client, store, queue, list_files, chunk_budget_tokens: int,
                     total_budget_tokens: int = 0) -> bool:
    """True — большой PR (fast-ack + fan-out чанков выполнены). False — small
    (вызывающий делает обычный одиночный проход)."""
    files = files_from_api(list_files(event.repo, event.number))
    sc, weight, plan = route(files, chunk_budget_tokens=chunk_budget_tokens,
                             total_budget_tokens=total_budget_tokens)
    if sc == SizeClass.SMALL or not plan or not plan.chunks:
        return False
    jk = job_key_for(event.repo, event.number, event.head_sha)
    ack.publish_ack(client, event.repo, event.number, weight, plan)   # ФТ-APRP-3
    # create_job идемпотентен → fan-out делает только первый (защита от двойной
    # доставки того же /review; describe сюда не заходит).
    if store.create_job(jk, event.head_sha, len(plan.chunks)):
        for p in build_chunk_payloads(event.repo, event.number, event.head_sha, jk, plan):
            queue.enqueue(p, pr_partition(event.repo, event.number))
        metrics.incr("mapreduce_fanout")
        logger.info("mapreduce fan-out: repo=%s pr=%d chunks=%d", event.repo,
                    event.number, len(plan.chunks))
    return True


def _progress_and_maybe_reduce(queue, store, client, p) -> None:
    jk, repo, number = p["job_key"], p["repo"], p["number"]
    st = store.job_status(jk) or {}
    ack.publish_progress_counts(client, repo, number, done=st.get("done_chunks", 0),
                                total=st.get("total_chunks", 0),
                                failed=st.get("failed_chunks", 0))
    if claim_reduce(store, jk):   # fan-in барьер (M4): ровно один enqueue reduce
        queue.enqueue({"event_type": REDUCE_EVENT, "job_key": jk, "repo": repo,
                       "number": number}, pr_partition(repo, number))
        metrics.incr("mapreduce_reduce_enqueued")


def _handle_chunk(lease, *, queue, store, client, review, max_attempts, backoff,
                  backpressure_delay) -> str:
    p = lease.payload
    jk, idx, files = p["job_key"], p["chunk_index"], p["files"]
    repo, number = p["repo"], p["number"]
    try:
        fwp = patches_for_files(client, repo, number, files)
        findings = review(fwp)                      # deep-tier; бросает при сбое
    except Backpressure:                            # локальный rate limit — отложить
        queue.defer(lease.id, lease.token, delay=backpressure_delay)
        metrics.incr("backpressure_deferred")
        return "deferred"
    except Exception as err:                        # сбой/таймаут ревью чанка
        eff = backoff * lease.attempts if backoff else 0
        outcome = queue.nack(lease.id, lease.token, max_attempts=max_attempts,
                             backoff=eff, reason=type(err).__name__)
        if outcome == "dead_letter":                # исчерпаны выдачи → чанк неуспешен
            store.record_chunk_finding(jk, idx, files, "", ok=False)   # НЕ молчим (partial)
            metrics.incr("mapreduce_chunk_dead_letter")
            logger.warning("mapreduce chunk DEAD-LETTER: repo=%s pr=%d chunk=%d reason=%s",
                           repo, number, idx, type(err).__name__)
            _progress_and_maybe_reduce(queue, store, client, p)
        return outcome
    store.record_chunk_finding(jk, idx, files, findings, ok=True)
    queue.ack(lease.id, lease.token)
    metrics.incr("mapreduce_chunk_ok")
    _progress_and_maybe_reduce(queue, store, client, p)
    return "ack"


def _handle_reduce(lease, *, queue, store, client, max_attempts, backoff) -> str:
    p = lease.payload
    jk, repo, number = p["job_key"], p["repo"], p["number"]
    try:
        reduce_mod.publish_review(client, repo, number, collect_results(store, jk))  # ФТ-APRP-8
    except Exception as err:                        # публикация не удалась → ретрай (upsert идемпотентен)
        eff = backoff * lease.attempts if backoff else 0
        return queue.nack(lease.id, lease.token, max_attempts=max_attempts,
                          backoff=eff, reason=type(err).__name__)
    queue.ack(lease.id, lease.token)
    metrics.incr("mapreduce_review_published")
    logger.info("mapreduce review published: repo=%s pr=%d", repo, number)
    return "ack"


def handle(lease, *, queue, store, client, review, max_attempts=5, backoff=0,
           backpressure_delay=5.0) -> str:
    """Диспетчер durable-события chunk/reduce (вызывается из worker.handle_lease)."""
    if lease.payload.get("event_type") == CHUNK_EVENT:
        return _handle_chunk(lease, queue=queue, store=store, client=client, review=review,
                             max_attempts=max_attempts, backoff=backoff,
                             backpressure_delay=backpressure_delay)
    return _handle_reduce(lease, queue=queue, store=store, client=client,
                          max_attempts=max_attempts, backoff=backoff)
