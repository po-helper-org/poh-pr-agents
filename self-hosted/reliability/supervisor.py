"""Обработка одного события — РОВНО одна попытка анализа (СТ-14..16).

Ретрай и эскалация (dead-letter → коммент в PR) вынесены на очередь+воркер, чтобы
не было двойного ретрая. process: success→DONE, ошибка→FAILED (без нотификации).
Идемпотентность по бизнес-ключу (СТ-16); force — reconcile доверяет GitHub-истине.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from reliability.state import Event, State, StateStore

Analyze = Callable[[Event], None]  # запускает анализ pr-agent; бросает при ошибке


@dataclass(frozen=True)
class Result:
    state: State
    attempts: int
    skipped: bool = False


def _drive_to_done(store: StateStore, delivery_id: str) -> None:
    """Довести свежее (RECEIVED) событие до DONE легальными переходами."""
    for target in (State.QUEUED, State.PROCESSING, State.DONE):
        if store.state_of(delivery_id) != target:
            store.transition(delivery_id, target)


def _ensure_processing(store: StateStore, delivery_id: str) -> None:
    """Драйв в PROCESSING из любого стартового состояния (RECEIVED/QUEUED/FAILED)."""
    cur = store.state_of(delivery_id)
    if cur == State.PROCESSING:
        return
    if cur in (State.RECEIVED, State.FAILED):
        store.transition(delivery_id, State.QUEUED)
    store.transition(delivery_id, State.PROCESSING)


def process(event: Event, analyze: Analyze, store: StateStore, *, force: bool = False) -> Result:
    # СТ-16: та же работа уже сделана. force=True — reconcile доверяет GitHub-истине.
    if not force and store.already_done(event.business_key):
        _drive_to_done(store, event.delivery_id)
        row = store.get(event.delivery_id)
        return Result(State.DONE, int(row["attempts"]) if row else 0, skipped=True)

    _ensure_processing(store, event.delivery_id)
    attempts = store.increment_attempt(event.delivery_id)
    try:
        analyze(event)
    except Exception:  # доменный сбой одной попытки; BaseException (отмена) — наверх
        store.transition(event.delivery_id, State.FAILED)
        return Result(State.FAILED, attempts)

    store.transition(event.delivery_id, State.DONE)
    return Result(State.DONE, attempts)
