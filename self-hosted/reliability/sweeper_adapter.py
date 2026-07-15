"""Реальные порты reconciliation sweeper (go-live).

- `has_completed_review` через state store (`already_done`) — надёжно ловит
  пропущенные webhook'и и необработанные PR (нет DONE-строки → reconcile).
  Детект «проглоченного» сбоя (DONE в сторе, но ревью на GitHub нет) требует
  проверки артефакта pr-agent на самом GitHub — followup, тюнится на смоуке.
- `list_open_prs` — открытые PR по настроенным репозиториям (GitHub API).

Парсинг и решение тестируемы; реальные HTTP-вызовы (`make_list_open_prs`) — pragma.
"""
from __future__ import annotations

from typing import Callable

from reliability.sweeper import OpenPR, business_key
from reliability.state import StateStore


def parse_open_prs(pulls_json: list, repo: str) -> list:
    out = []
    for pr in pulls_json:
        number = pr.get("number")
        head_sha = (pr.get("head") or {}).get("sha")
        if number is not None and head_sha:
            out.append(OpenPR(repo=repo, number=int(number), head_sha=head_sha))
    return out


def make_has_completed_review(store: StateStore) -> Callable[[str, int, str, str], bool]:
    def has_completed_review(repo: str, number: int, head_sha: str, command: str) -> bool:
        return store.already_done(business_key(repo, number, head_sha, command))
    return has_completed_review


def make_list_open_prs(client, repos):  # pragma: no cover - реальные вызовы GitHub
    def list_open_prs():
        prs = []
        for repo in repos:
            prs.extend(parse_open_prs(client.list_open_pulls(repo), repo))
        return prs
    return list_open_prs
