"""Логика приёма webhook — вынесена из FastAPI-обвязки для тестируемости.

Возвращает HTTP-статус; побочный эффект — регистрация события в сторе (СТ-2) и
планирование обработки через `schedule`. Защищает от неподписанного (401),
битого/неполного payload (400) — чтобы отказ не превращался в 500 + бесконечные
ретраи доставки (риск против К-1). Тестируется без FastAPI.
"""
from __future__ import annotations

import json
import logging
from typing import Callable

from reliability.security import verify_signature
from reliability.state import Event, StateStore
from reliability.webhook import parse_events

Enrich = Callable[[list], list]  # list[Event] -> list[Event]; обогащение head_sha (СТ-8)

logger = logging.getLogger(__name__)  # reliability.ingress → stdout (см. logging_setup)


def handle_webhook(raw: bytes, headers: dict, *, secret: str,
                   store: StateStore, schedule: Callable[[Event], None],
                   enrich: Enrich = lambda evs: evs) -> int:
    h = {k.lower(): v for k, v in headers.items()}
    event_type = h.get("x-github-event", "")
    delivery = h.get("x-github-delivery", "")
    if not verify_signature(secret, raw, h.get("x-hub-signature-256")):
        logger.warning("webhook rejected 401: bad signature (event=%s delivery=%s)",
                       event_type, delivery)
        return 401  # СТ-1
    try:
        payload = json.loads(raw or b"{}")
    except (ValueError, TypeError):
        logger.warning("webhook rejected 400: malformed JSON (event=%s delivery=%s)",
                       event_type, delivery)
        return 400  # битое тело — явный отказ, не 500
    if not isinstance(payload, dict):
        logger.warning("webhook rejected 400: payload not an object (event=%s delivery=%s)",
                       event_type, delivery)
        return 400
    action = payload.get("action")
    logger.info("webhook received: event=%s action=%s delivery=%s",
                event_type, action, delivery)
    try:
        events = parse_events(event_type, delivery, payload)
        # обогащение head_sha ДО record_received: business_key зависит от sha
        # (issue_comment приходит без sha). Дефолт — identity (PR уже несёт sha).
        events = enrich(events)
    except (KeyError, TypeError):
        logger.warning("webhook rejected 400: parse error (event=%s delivery=%s)",
                       event_type, delivery)
        return 400  # подстраховка: parse_events и так устойчив к пропускам полей
    enqueued = deduped = 0
    for event in events:
        if store.record_received(event):  # СТ-2 dedup
            schedule(event)
            enqueued += 1
        else:
            deduped += 1
    logger.info("webhook accepted 200: event=%s parsed=%d enqueued=%d deduped=%d delivery=%s",
                event_type, len(events), enqueued, deduped, delivery)
    return 200
