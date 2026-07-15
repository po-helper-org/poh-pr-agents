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

    def _list_comments(self, repo: str, number: int) -> list:
        s, b = self._transport(
            "GET", f"{self._api}/repos/{repo}/issues/{number}/comments", None, self._headers(repo))
        if s >= 300:
            raise RuntimeError(f"list comments {s}: {b[:200]!r}")
        return json.loads(b)

    def upsert_comment(self, repo: str, number: int, marker: str, body: str) -> None:
        """СТ-25: правит существующий коммент с маркером, иначе создаёт новый."""
        tagged = f"{body}\n\n{marker}"
        existing = next(
            (c for c in self._list_comments(repo, number) if marker in (c.get("body") or "")),
            None,
        )
        data = json.dumps({"body": tagged}).encode()
        if existing is not None:
            s, b = self._transport(
                "PATCH", f"{self._api}/repos/{repo}/issues/comments/{existing['id']}",
                data, self._headers(repo))
        else:
            s, b = self._transport(
                "POST", f"{self._api}/repos/{repo}/issues/{number}/comments",
                data, self._headers(repo))
        if s >= 300:
            raise RuntimeError(f"upsert comment {s}: {b[:200]!r}")
