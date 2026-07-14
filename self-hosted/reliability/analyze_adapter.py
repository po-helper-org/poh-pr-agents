"""Интеграционный шов: запуск анализа pr-agent + минтинг installation-токена.

Оркестрация тестируется (`PRAgentAnalyzer`, `_pr_url`); реальные вызовы pr-agent,
crypto-подпись JWT и urllib-транспорт — тонкие обёртки (`pragma: no cover`),
проверяются на первом деплое. Как поднять — см. reliability/README.md.
"""
from __future__ import annotations

import base64
import os
import time
import urllib.request
from typing import Callable, Optional

from reliability.state import Event
from reliability.token import InstallationTokenProvider


def _pr_url(event: Event) -> str:
    return f"https://github.com/{event.repo}/pull/{event.number}"


class PRAgentAnalyzer:
    """Запускает анализ pr-agent для события; пробрасывает исключение при сбое.

    Случай, когда pr-agent проглотил ошибку и вернулся штатно (без исключения),
    здесь НЕ детектируется — его закрывает reconciliation sweeper (Фаза 3),
    проверяющий наличие ревью по head_sha. Здесь — только hard-failure с raise.
    """

    def __init__(self, invoke: Callable[[str, str], None]):
        self._invoke = invoke

    def run(self, event: Event) -> None:
        self._invoke(_pr_url(event), event.command)


# --- реальные обёртки (pragma: no cover — проверяются на деплое) ---

def _real_invoke(pr_url: str, command: str) -> None:  # pragma: no cover
    import asyncio

    from pr_agent.agent.pr_agent import PRAgent
    asyncio.run(PRAgent().handle_request(pr_url, command))


def _urllib_transport(method: str, url: str, headers: dict,
                      data: Optional[bytes]) -> "tuple[int, bytes]":  # pragma: no cover
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status, resp.read()


def _pyjwt_signer(app_id: str, pem: str, iat: int, exp: int) -> str:  # pragma: no cover
    import jwt  # PyJWT — есть в контейнере pr-agent
    return jwt.encode({"iat": iat, "exp": exp, "iss": app_id}, pem, algorithm="RS256")


def _default_provider() -> InstallationTokenProvider:  # pragma: no cover
    app_id = os.environ.get("GITHUB_APP_ID", "")
    pem_b64 = os.environ.get("GITHUB_PRIVATE_KEY_B64", "")
    pem = base64.b64decode(pem_b64).decode() if pem_b64 else ""
    return InstallationTokenProvider(app_id, pem, _urllib_transport, _pyjwt_signer,
                                     clock=time.time)


# --- публичный интерфейс для app.py ---

run = PRAgentAnalyzer(_real_invoke).run

_provider: Optional[InstallationTokenProvider] = None


def installation_token(repo: str) -> str:  # pragma: no cover
    global _provider
    if _provider is None:
        _provider = _default_provider()
    return _provider.get(repo)
