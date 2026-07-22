"""Обработка одного события — РОВНО одна попытка анализа (СТ-14..16).

Ретрай и эскалация (dead-letter → коммент в PR) вынесены на очередь+воркер, чтобы
не было двойного ретрая. process: success→DONE, ошибка→FAILED (без нотификации).
Идемпотентность по бизнес-ключу (СТ-16); force — reconcile доверяет GitHub-истине.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from reliability.state import Backpressure, Event, State, StateStore

Analyze = Callable[[Event], None]  # запускает анализ pr-agent; бросает при ошибке


@dataclass(frozen=True)
class Result:
    state: State
    attempts: int
    skipped: bool = False
    error: "str | None" = None  # класс исключения при FAILED — для точного reason в DLQ-комменте


def _drive_to_done(store: StateStore, delivery_id: str) -> None:
    """No-op если событие уже терминал — защита от передоставки DONE-события
    (воркер упал после DONE, но до ack); иначе довести до DONE легальными переходами."""
    if store.state_of(delivery_id) in (State.DONE, State.DEAD_LETTER):
        return
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

    # СТ-16: атомарный захват бизнес-ключа. Две конкурентные доставки одного
    # ключа (напр. повторная доставка webhook с новым delivery_id) не должны
    # запустить анализ дважды — comment-дубль закрыт upsert (СТ-25), но сам
    # анализ повторялся. Проигравший — skip; его строка останется вне терминала
    # и будет добита already_done/свипером после победителя (без тихой потери).
    # force (reconcile) тоже уважает захват: свежий in-flight важнее reconcile.
    if not store.try_claim(event.business_key, event.delivery_id):
        row = store.get(event.delivery_id)
        cur = store.state_of(event.delivery_id) or State.RECEIVED
        return Result(cur, int(row["attempts"]) if row else 0, skipped=True)

    _ensure_processing(store, event.delivery_id)
    attempts = store.increment_attempt(event.delivery_id)
    try:
        analyze(event)
    except Backpressure:
        # НЕ сбой попытки: локальный rate limit ИЛИ системный простой провайдера
        # (все цепи разомкнуты — GatewayCircuitOpen). Не метим FAILED, не публикуем
        # провал — отдаём наверх, воркер отложит без счёта к DLQ. Захват держим
        # (re-entrant при передоставке того же delivery_id).
        raise
    except Exception as exc:  # доменный сбой одной попытки; BaseException (отмена) — наверх
        store.transition(event.delivery_id, State.FAILED)
        store.release_claim(event.business_key, event.delivery_id)  # дать ретраю пере-захват
        return Result(State.FAILED, attempts, error=type(exc).__name__)

    store.transition(event.delivery_id, State.DONE)
    store.release_claim(event.business_key, event.delivery_id)
    return Result(State.DONE, attempts)
