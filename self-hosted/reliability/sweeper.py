"""Reconciliation sweeper (СТ-13, 29..32): периодическая сверка и дозапуск.

Ловит пропущенные webhook'и, необработанные PR и застрявших воркеров — и
дозапускает. Насколько строг критерий «есть ревью» — определяет инъектируемый
порт `has_completed_review`; текущая go-live реализация (`sweeper_adapter`)
проверяет DONE-строку в store, чем закрывает пропуск/застревание. Детект
«проглоченного» сбоя (DONE в сторе, но ревью на GitHub нет) — followup: тот же
порт, но со сверкой артефакта на GitHub (тюнится на смоуке). Порты инъектируются
→ тестируется без GitHub/сети.

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

    # 1) СТ-13: застрявшие вне терминала. FAILED — тоже не терминал и попадает сюда,
    # поэтому переходы делаем с учётом текущего состояния (без FAILED->FAILED).
    for row in store.stale(stale_deadline):
        did = row["delivery_id"]
        attempts = int(row["attempts"])
        cur = State(row["state"])
        if attempts >= max_attempts:
            if cur != State.FAILED:
                store.transition(did, State.FAILED)
            store.transition(did, State.DEAD_LETTER)  # довести до терминала
            metrics.incr("dead_letter_total")
            notify_failure(client, _event_from_row(row, did, row["event_type"]),
                           StuckTimeout("stuck beyond deadline"), attempts, escalated=True)
            rep.dead_lettered.append(did)
        else:
            # claim: вернуть ту же строку в очередь легальными переходами (обновляет
            # timestamp → покидает stale-выборку), затем повторно обработать её же.
            if cur == State.PROCESSING:
                store.transition(did, State.FAILED)
                cur = State.FAILED
            if cur in (State.RECEIVED, State.FAILED):
                store.transition(did, State.QUEUED)
            enqueue(_event_from_row(row, did, row["event_type"]))
            metrics.incr("reconcile_requeues")
            rep.requeued.append(did)

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
                    client.upsert_comment(
                        pr.repo, pr.number, f"<!-- reliability:reconcile:{cmd} -->",
                        f"⚠️ Автоматические попытки получить `{cmd}` исчерпаны "
                        f"({max_cycles} циклов). Требуется ручной запуск: `{cmd}`.")
                    rep.escalated.append(bkey)
                continue
            # reconcile-событие с force: обходим already_done (has_completed_review
            # уже сказал «ревью нет» — истина порта, для go-live это store).
            # id на монотонном seq — не коллизится при флапе has_completed_review.
            rec = Event(delivery_id=f"reconcile:{bkey}:{store.next_seq()}", repo=pr.repo,
                        number=pr.number, head_sha=pr.head_sha, command=cmd,
                        event_type="reconcile")
            if store.record_received(rec):
                enqueue(rec, force=True)
                metrics.incr("reconcile_requeues")
                rep.reconciled.append(bkey)

    return rep
