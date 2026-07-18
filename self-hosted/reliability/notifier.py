"""Видимое оповещение о провале в PR/issue (СТ-27).

Ключевой пункт «не молчать»: когда обработка не удалась, оставляем
человекочитаемый след в самом PR — upstream pr-agent этого не делает.
GitHub-клиент инъектируется (Protocol) — модуль тестируется без сети.
"""
from __future__ import annotations

from typing import Protocol


class GitHubClient(Protocol):
    def upsert_comment(self, repo: str, number: int, marker: str, body: str) -> None: ...


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


def notify_failure(client: GitHubClient, event, error, attempts: int, escalated: bool) -> str:
    """Идемпотентно публикует комментарий о провале (СТ-25 upsert) и возвращает тело.

    Один коммент на (PR, команда): ретраи/reconcile правят его, а не плодят дубли.
    `error` — исключение ИЛИ строка-класс причины (воркер отдаёт точный класс сбоя,
    напр. GatewayUnavailable vs Z.AI-ошибка, чтобы коммент/метрика не врали, К-5).
    """
    error_class = error if isinstance(error, str) else type(error).__name__
    body = build_failure_comment(event.command, error_class, attempts, escalated)
    marker = f"<!-- reliability:failure:{event.command} -->"
    client.upsert_comment(event.repo, event.number, marker, body)
    return body
