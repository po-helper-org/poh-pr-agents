"""Интеграционный шов: минтинг installation-токена + запуск анализа pr-agent.

РЕАЛИЗУЕТСЯ СЛЕДУЮЩИМ СРЕЗОМ — до этого ingress не разворачивается end-to-end.
В контейнере pr-agent доступны PyJWT/cryptography для App JWT → installation token.

- installation_token(repo): App JWT (App ID + private key) → installation token.
- run(event): триггерит анализ pr-agent для (repo, number, command).
"""
from __future__ import annotations

from reliability.state import Event


def installation_token(repo: str) -> str:  # pragma: no cover - интеграционный шов
    raise NotImplementedError("минтинг installation-токена — следующий срез (issue #1)")


def run(event: Event) -> None:  # pragma: no cover - интеграционный шов
    raise NotImplementedError("запуск анализа pr-agent — следующий срез (issue #1)")
