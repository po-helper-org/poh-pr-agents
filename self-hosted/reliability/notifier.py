"""Видимое оповещение о провале в PR/issue (СТ-27).

Ключевой пункт «не молчать»: когда обработка не удалась, оставляем
человекочитаемый след в самом PR — upstream pr-agent этого не делает.
GitHub-клиент инъектируется (Protocol) — модуль тестируется без сети.
"""
from __future__ import annotations

from typing import Protocol


class GitHubClient(Protocol):
    def post_issue_comment(self, repo: str, number: int, body: str) -> None: ...


def build_failure_comment(command: str, error_class: str, attempts: int, escalated: bool) -> str:
    """Тело комментария о провале. `escalated` — событие ушло в dead-letter."""
    head = f"⚠️ Автоматический `{command}` не выполнен."
    detail = f"Причина: `{error_class}`. Попыток: {attempts}."
    if escalated:
        tail = (
            "Событие эскалировано (dead-letter) и будет повторено автоматически "
            f"reconciliation-свипером. Можно перезапустить вручную — напишите `{command}` комментарием."
        )
    else:
        tail = f"Идёт автоматический повтор. Если результат не появится — запустите вручную: `{command}`."
    return f"{head}\n\n{detail}\n\n{tail}"


def notify_failure(client: GitHubClient, event, error: BaseException, attempts: int, escalated: bool) -> str:
    """Публикует комментарий о провале и возвращает его тело (для лога/теста)."""
    body = build_failure_comment(event.command, type(error).__name__, attempts, escalated)
    client.post_issue_comment(event.repo, event.number, body)
    return body
