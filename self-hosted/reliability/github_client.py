"""GitHub-клиент для публикации комментариев (СТ-27 delivery).

Токен установки и HTTP-транспорт инъектируются → модуль тестируется без сети и
без крипто-зависимостей. Дефолтный транспорт — stdlib urllib (zero-dep).
Минтинг installation-токена — интеграционный шов `analyze_adapter` (след. срез).
"""
from __future__ import annotations

import json
import urllib.request
from typing import Callable

# (url, data, headers) -> (status_code, body_bytes)
Transport = Callable[[str, bytes, dict], "tuple[int, bytes]"]


def _urllib_transport(url: str, data: bytes, headers: dict) -> "tuple[int, bytes]":
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:  # pragma: no cover - сеть
        return resp.status, resp.read()


class GitHubAppClient:
    def __init__(self, token_provider: Callable[[str], str],
                 api_base: str = "https://api.github.com",
                 transport: Transport = _urllib_transport):
        self._token_provider = token_provider   # (repo) -> installation token
        self._api = api_base.rstrip("/")
        self._transport = transport

    def post_issue_comment(self, repo: str, number: int, body: str) -> None:
        url = f"{self._api}/repos/{repo}/issues/{number}/comments"
        data = json.dumps({"body": body}).encode()
        headers = {
            "Authorization": f"Bearer {self._token_provider(repo)}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "pr-agent-reliability",
        }
        status, resp = self._transport(url, data, headers)
        if status >= 300:
            raise RuntimeError(f"GitHub API {status}: {resp[:200]!r}")
