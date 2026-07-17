"""GitHub-клиент: идемпотентная публикация комментария (СТ-25, upsert).

`upsert_comment` правит существующий бот-коммент, найденный по скрытому маркеру
`<!-- reliability:... -->`, либо создаёт новый — так ретраи/reconcile не плодят
дубликаты. Токен установки и HTTP-транспорт (method-aware) инъектируются →
тестируется без сети и крипто. Дефолтный транспорт — stdlib urllib.
"""
from __future__ import annotations

import json
import urllib.request
from typing import Callable, Optional

# (method, url, data, headers) -> (status_code, body_bytes)
Transport = Callable[[str, str, "Optional[bytes]", dict], "tuple[int, bytes]"]


def _urllib_transport(method: str, url: str, data: "Optional[bytes]", headers: dict):  # pragma: no cover
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status, resp.read()


class GitHubAppClient:
    def __init__(self, token_provider: Callable[[str], str],
                 api_base: str = "https://api.github.com",
                 transport: Transport = _urllib_transport):
        self._token_provider = token_provider
        self._api = api_base.rstrip("/")
        self._transport = transport

    def _headers(self, repo: str) -> dict:
        return {
            "Authorization": f"Bearer {self._token_provider(repo)}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "pr-agent-reliability",
        }

    def _matching_comments(self, repo: str, number: int, marker: str) -> list:
        """Комменты БОТА с маркером по всем страницам (пагинация — иначе на PR с
        >30 комментами наш коммент не находится и плодится дубль). Фильтр по
        user.type=='Bot', чтобы чужой процитированный маркер не матчился."""
        found, page = [], 1
        while True:
            s, b = self._transport(
                "GET",
                f"{self._api}/repos/{repo}/issues/{number}/comments?per_page=100&page={page}",
                None, self._headers(repo))
            if s >= 300:
                raise RuntimeError(f"list comments {s}: {b[:200]!r}")
            items = json.loads(b)
            for c in items:
                if marker in (c.get("body") or "") and (c.get("user") or {}).get("type") == "Bot":
                    found.append(c)
            if len(items) < 100:
                return found
            page += 1

    def get_pull_head_sha(self, repo: str, number: int) -> str:
        """head SHA открытого PR по номеру — для обогащения issue_comment-событий
        (в их payload sha нет). Пусто, если номер — issue, а не PR (404), или ошибка:
        обогащение тогда отбросит событие, а не сохранит с пустым ключом."""
        s, b = self._transport(
            "GET", f"{self._api}/repos/{repo}/pulls/{number}", None, self._headers(repo))
        if s == 404:
            return ""  # issue, не PR — ревьюить нечего
        if s >= 300:
            raise RuntimeError(f"get pull {s}: {b[:200]!r}")
        return ((json.loads(b).get("head") or {}).get("sha")) or ""

    def list_open_pulls(self, repo: str) -> list:
        """Открытые PR репозитория (по всем страницам) — для reconciliation sweeper."""
        out, page = [], 1
        while True:
            s, b = self._transport(
                "GET", f"{self._api}/repos/{repo}/pulls?state=open&per_page=100&page={page}",
                None, self._headers(repo))
            if s >= 300:
                raise RuntimeError(f"list pulls {s}: {b[:200]!r}")
            items = json.loads(b)
            out.extend(items)
            if len(items) < 100:
                return out
            page += 1

    def has_bot_activity(self, repo: str, number: int) -> bool:
        """Есть ли на PR хоть один коммент от бота — опорное доказательство того,
        что ревью опубликовано (для детекта «проглоченного» сбоя в свипере). Точную
        эвристику артефакта донастроить на смоуке; пагинация как в _matching_comments."""
        page = 1
        while True:
            s, b = self._transport(
                "GET",
                f"{self._api}/repos/{repo}/issues/{number}/comments?per_page=100&page={page}",
                None, self._headers(repo))
            if s >= 300:
                raise RuntimeError(f"list comments {s}: {b[:200]!r}")
            items = json.loads(b)
            if any((c.get("user") or {}).get("type") == "Bot" for c in items):
                return True
            if len(items) < 100:
                return False
            page += 1

    def upsert_comment(self, repo: str, number: int, marker: str, body: str) -> None:
        """СТ-25: правит существующий бот-коммент с маркером, иначе создаёт новый.
        Лишние дубликаты (от гонок) схлопывает — идемпотентность самовосстанавливается."""
        tagged = f"{body}\n\n{marker}"
        data = json.dumps({"body": tagged}).encode()
        matches = self._matching_comments(repo, number, marker)
        if not matches:
            s, b = self._transport(
                "POST", f"{self._api}/repos/{repo}/issues/{number}/comments",
                data, self._headers(repo))
            if s >= 300:
                raise RuntimeError(f"create comment {s}: {b[:200]!r}")
            return
        s, b = self._transport(
            "PATCH", f"{self._api}/repos/{repo}/issues/comments/{matches[0]['id']}",
            data, self._headers(repo))
        if s >= 300:
            raise RuntimeError(f"update comment {s}: {b[:200]!r}")
        for extra in matches[1:]:  # self-heal: удалить дубли, оставить один
            self._transport(
                "DELETE", f"{self._api}/repos/{repo}/issues/comments/{extra['id']}",
                None, self._headers(repo))
