"""Reliability-супервизор: оркестрация обработки одного события (СТ-14..16, 27).

Оборачивает анализ pr-agent так, чтобы провал НЕ был тихим:
- успех → DONE;
- сбой ниже порога → FAILED (будет повторён очередью/свипером — след. срез);
- сбой на пороге → DEAD_LETTER + видимый комментарий в PR (СТ-27).
Идемпотентность по бизнес-ключу (СТ-16): если работа уже сделана — no-op DONE.

`analyze` и `client` инъектируются → полностью тестируется без сети и pr-agent.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from reliability.notifier import GitHubClient, notify_failure
from reliability.state import Event, State, StateStore

Analyze = Callable[[Event], None]  # запускает анализ pr-agent; бросает при ошибке


@dataclass(frozen=True)
class Result:
    state: State
    attempts: int
    notified: bool
    skipped: bool = False


def _drive_to_done(store: StateStore, delivery_id: str) -> None:
    """Довести свежее (RECEIVED) событие до DONE легальными переходами."""
    for target in (State.QUEUED, State.PROCESSING, State.DONE):
        if store.state_of(delivery_id) != target:
            store.transition(delivery_id, target)


def process(event: Event, analyze: Analyze, store: StateStore,
            client: GitHubClient, *, max_attempts: int = 5) -> Result:
    # СТ-16: та же работа (repo,number,head_sha,command) уже сделана другим delivery
    if store.already_done(event.business_key):
        _drive_to_done(store, event.delivery_id)
        row = store.get(event.delivery_id)
        return Result(State.DONE, int(row["attempts"]) if row else 0, notified=False, skipped=True)

    store.transition(event.delivery_id, State.QUEUED)
    store.transition(event.delivery_id, State.PROCESSING)
    attempts = store.increment_attempt(event.delivery_id)

    try:
        analyze(event)
    except BaseException as err:  # noqa: BLE001 — любой сбой обязан быть виден
        store.transition(event.delivery_id, State.FAILED)
        if attempts >= max_attempts:
            store.transition(event.delivery_id, State.DEAD_LETTER)
            notify_failure(client, event, err, attempts, escalated=True)  # СТ-27
            return Result(State.DEAD_LETTER, attempts, notified=True)
        return Result(State.FAILED, attempts, notified=False)

    store.transition(event.delivery_id, State.DONE)
    return Result(State.DONE, attempts, notified=False)
