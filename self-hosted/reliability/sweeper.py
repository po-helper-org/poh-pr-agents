"""Reconciliation sweeper (СТ-13, 29..32): периодическая сверка и дозапуск.

Настоящая гарантия «не молчать»: ловит пропущенные webhook'и, застрявших воркеров
и «проглоченные» сбои (pr-agent вернулся штатно, но ревью на head_sha нет) —
и дозапускает. Порты инъектируются → тестируется без GitHub/сети.

За один проход:
1) застрявшие вне терминала события (СТ-13) → свежий retry или dead-letter;
2) открытые PR без подтверждённого ревью (СТ-29/31) → reconcile-enqueue (force);
3) эскалация после max_cycles циклов (СТ-32) — не бесконечная тихая петля.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from reliability import metrics
from reliability.notifier import GitHubClient, notify_failure
from reliability.state import Event, State, StateStore


class StuckTimeout(Exception):
    """Событие застряло вне терминала дольше deadline (СТ-13)."""


@dataclass(frozen=True)
class OpenPR:
    repo: str
    number: int
    head_sha: str


@dataclass
class SweepReport:
    requeued: list = field(default_factory=list)
    dead_lettered: list = field(default_factory=list)
    reconciled: list = field(default_factory=list)
    escalated: list = field(default_factory=list)


def business_key(repo: str, number: int, head_sha: str, command: str) -> str:
    return f"{repo}#{number}@{head_sha}:{command}"


def _event_from_row(row, delivery_id: str, event_type: str) -> Event:
    return Event(delivery_id=delivery_id, repo=row["repo"], number=int(row["number"]),
                 head_sha=row["head_sha"], command=row["command"], event_type=event_type)


def sweep(store: StateStore, *,
          list_open_prs: Callable[[], list],
          has_completed_review: Callable[[str, int, str, str], bool],
          enqueue: Callable[..., None],
          client: GitHubClient,
          commands,
          stale_deadline: float,
          max_attempts: int,
          max_cycles: int) -> SweepReport:
    rep = SweepReport()

    # 1) СТ-13: застрявшие вне терминала — не реанимируем строку, а заводим свежую
    for row in store.stale(stale_deadline):
        did = row["delivery_id"]
        attempts = int(row["attempts"])
        store.transition(did, State.FAILED)  # RECEIVED/QUEUED/PROCESSING -> FAILED легально
        if attempts >= max_attempts:
            store.transition(did, State.DEAD_LETTER)
            metrics.incr("dead_letter_total")
            notify_failure(client, _event_from_row(row, did, row["event_type"]),
                           StuckTimeout("stuck beyond deadline"), attempts, escalated=True)
            rep.dead_lettered.append(did)
        else:
            retry = _event_from_row(row, f"retry:{did}:{attempts}", "retry")
            if store.record_received(retry):
                enqueue(retry)  # обычный путь: already_done корректно отразит эффект
                rep.requeued.append(retry.delivery_id)

    # 2) СТ-29/31: открытые PR без подтверждённого ревью → reconcile
    for pr in list_open_prs():
        for cmd in commands:
            bkey = business_key(pr.repo, pr.number, pr.head_sha, cmd)
            if has_completed_review(pr.repo, pr.number, pr.head_sha, cmd):
                store.clear_reconcile(bkey)   # эффект есть — сбросить счётчик
                continue
            if store.in_flight(bkey):
                continue                       # СТ-30: уже в работе, не дублируем
            cycles = store.bump_reconcile(bkey)
            if cycles > max_cycles:            # СТ-32: эскалация
                if cycles == max_cycles + 1:   # оповещаем один раз, дальше молча стоп
                    metrics.incr("reconcile_escalated_total")
                    client.post_issue_comment(
                        pr.repo, pr.number,
                        f"⚠️ Автоматические попытки получить `{cmd}` исчерпаны "
                        f"({max_cycles} циклов). Требуется ручной запуск: `{cmd}`.")
                    rep.escalated.append(bkey)
                continue
            # reconcile-событие с force: GitHub — истина, обходим already_done
            rec = Event(delivery_id=f"reconcile:{bkey}:{cycles}", repo=pr.repo,
                        number=pr.number, head_sha=pr.head_sha, command=cmd,
                        event_type="reconcile")
            if store.record_received(rec):
                enqueue(rec, force=True)
                rep.reconciled.append(bkey)

    return rep
