"""Разбор webhook GitHub → список Event (СТ-8).

Одна доставка PR-события может породить несколько команд (describe+review) —
для каждой формируется отдельный Event с уникальным ключом идемпотентности
`{delivery_id}:{command}` (чтобы dedup и PK работали покомандно).
"""
from __future__ import annotations

from dataclasses import replace
from typing import Callable

from reliability.state import Event

DEFAULT_PR_COMMANDS = ("/describe", "/review")

# (repo, number) -> head_sha ("" если issue не является PR / API недоступен)
FetchHeadSha = Callable[[str, int], str]


def enrich_events(events: list[Event], fetch_head_sha: FetchHeadSha) -> list[Event]:
    """Дозаполнить head_sha для событий без него (issue_comment: в payload sha нет).

    Ключ идемпотентности (business_key) зависит от head_sha, поэтому обогащение
    ОБЯЗАНО выполняться до record_received/enqueue — иначе строка сохранится с
    пустым sha и reconcile/дедуп по push сломаются. События, для которых sha так и
    не получен (issue — не PR, либо API молчит), отбрасываются: ревьюить нечего,
    а неполный ключ хуже отсутствия события. PR-события уже несут sha → проходят
    без обращения к API.
    """
    out: list[Event] = []
    for e in events:
        if e.head_sha:
            out.append(e)
            continue
        sha = fetch_head_sha(e.repo, e.number)
        if sha:
            out.append(replace(e, head_sha=sha))
    return out
PR_TRIGGER_ACTIONS = frozenset({"opened", "reopened", "ready_for_review", "synchronize"})


def parse_events(event_type: str, delivery_id: str, payload: dict,
                 pr_commands=DEFAULT_PR_COMMANDS) -> list[Event]:
    if event_type == "pull_request":
        if payload.get("action") not in PR_TRIGGER_ACTIONS:
            return []
        pr = payload.get("pull_request") or {}
        repo = (payload.get("repository") or {}).get("full_name")
        number = pr.get("number")
        head_sha = (pr.get("head") or {}).get("sha")
        if not (repo and number is not None and head_sha):
            return []  # неполный payload — не выдумываем событие
        return [
            Event(delivery_id=f"{delivery_id}:{cmd}", repo=repo, number=number,
                  head_sha=head_sha, command=cmd, event_type=event_type)
            for cmd in pr_commands
        ]

    if event_type == "issue_comment":
        if payload.get("action") != "created":
            return []
        body = ((payload.get("comment") or {}).get("body") or "").strip()
        if not body.startswith("/"):
            return []
        cmd = body.split()[0]
        repo = (payload.get("repository") or {}).get("full_name")
        number = (payload.get("issue") or {}).get("number")
        if not (repo and number is not None):
            return []
        # head_sha из payload issue_comment недоступен напрямую — обогащается на
        # следующем шаге (запрос PR по номеру); пока пусто (см. issue #1).
        head_sha = payload.get("_head_sha", "")
        return [Event(delivery_id=f"{delivery_id}:{cmd}", repo=repo, number=number,
                      head_sha=head_sha, command=cmd, event_type=event_type)]

    return []
