"""Логика приёма webhook — вынесена из FastAPI-обвязки для тестируемости.

Возвращает HTTP-статус; побочный эффект — регистрация события в сторе (СТ-2) и
планирование обработки через `schedule`. Защищает от неподписанного (401),
битого/неполного payload (400) — чтобы отказ не превращался в 500 + бесконечные
ретраи доставки (риск против К-1). Тестируется без FastAPI.
"""
from __future__ import annotations

import json
from typing import Callable

from reliability.security import verify_signature
from reliability.state import Event, StateStore
from reliability.webhook import parse_events

Enrich = Callable[[list], list]  # list[Event] -> list[Event]; обогащение head_sha (СТ-8)


def handle_webhook(raw: bytes, headers: dict, *, secret: str,
                   store: StateStore, schedule: Callable[[Event], None],
                   enrich: Enrich = lambda evs: evs) -> int:
    h = {k.lower(): v for k, v in headers.items()}
    if not verify_signature(secret, raw, h.get("x-hub-signature-256")):
        return 401  # СТ-1
    try:
        payload = json.loads(raw or b"{}")
    except (ValueError, TypeError):
        return 400  # битое тело — явный отказ, не 500
    if not isinstance(payload, dict):
        return 400
    try:
        events = parse_events(h.get("x-github-event", ""),
                              h.get("x-github-delivery", ""), payload)
        # обогащение head_sha ДО record_received: business_key зависит от sha
        # (issue_comment приходит без sha). Дефолт — identity (PR уже несёт sha).
        events = enrich(events)
    except (KeyError, TypeError):
        return 400  # подстраховка: parse_events и так устойчив к пропускам полей
    for event in events:
        if store.record_received(event):  # СТ-2 dedup
            schedule(event)
    return 200
