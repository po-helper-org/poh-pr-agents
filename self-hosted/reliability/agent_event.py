"""Доклад PR-Agent в цикл Issue (точка передачи H3 протокола агентов).

Issue-Agent держит задачу живым Temporal-workflow и после открытия PR ждёт в
фазе `pr-open`. Без доклада отсюда он там и остаётся: своих вебхуков по PR он не
слушает намеренно — фазу двигает тот, кто выполнил работу, а не тот, кто мимо
проходил.

Контракт узкий и односторонний: `POST /agent-event` с HMAC-подписью, конверт
`{repo, agent, phase, status, ref, root_issue?, detail?}`. Temporal сюда не
втаскивается — у этого сервиса свой релизный цикл, и знание чужих workflow id
было бы ровно той связностью, ради ухода от которой контракт и заводился.

Доклад — вспомогательный канал, и он НИКОГДА не роняет обработку. Ревью уже
опубликовано; уронить его из-за недоступного соседа значило бы обменять
сделанную работу на несделанный доклад.

Секрет и адрес берутся из окружения. Не задан любой из двух — канал выключен
целиком: это и есть процедура отката.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import urllib.request
from typing import Callable, Optional

logger = logging.getLogger("reliability.agent_event")

# (method, url, data, headers) -> (status, body) — тот же Transport, что и в
# github_client: модуль тестируется без сети.
Transport = Callable[[str, str, "Optional[bytes]", dict], "tuple[int, bytes]"]

AGENT = "pr-agent"

# Фаза, о которой докладывает именно этот сервис. Другие фазы пути (`merged`,
# `testing`) ведут соседи, и присваивать их себе означало бы врать таймлайну.
PHASE = "pr-review"

STARTED = "started"
FAILED = "failed"

# Формы, которые GitHub считает закрывающими. Держим их же: расхождение означало
# бы, что PR закрывает Issue, а контур этого не видит.
_CLOSES_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*#(\d+)", re.IGNORECASE)


def _urllib_transport(method: str, url: str, data, headers):  # pragma: no cover
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, resp.read()


def parse_root_issue(pr_body: str | None) -> Optional[int]:
    """Номер задачи из `Closes #N` в теле PR, иначе None.

    Несколько закрываемых Issue — НЕ повод выбрать первый: привязать работу не к
    той задаче значит испортить трассировку сразу двум. Такой случай уходит без
    ключа, и Issue-Agent разберётся сам (у него та же развилка) либо запишет
    сироту.
    """
    if not pr_body:
        return None
    found = {int(n) for n in _CLOSES_RE.findall(pr_body)}
    return found.pop() if len(found) == 1 else None


def build_payload(repo: str, pr_number: int, status: str, *,
                  root_issue: Optional[int] = None, detail: str = "") -> dict:
    payload = {
        "repo": repo,
        "agent": AGENT,
        "phase": PHASE,
        "status": status,
        # `ref` — то, о чём событие. Номер PR: он же половина ключа
        # идемпотентности на стороне цикла, поэтому строкой и без префикса.
        "ref": str(pr_number),
        "detail": detail,
    }
    if root_issue is not None:
        payload["root_issue"] = root_issue
    return payload


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def configured(env=None) -> bool:
    env = env if env is not None else os.environ
    return bool(env.get("ISSUE_AGENT_URL", "").strip()
                and env.get("AGENT_EVENT_SECRET", "").strip())


def report(repo: str, pr_number: int, status: str, *, root_issue: Optional[int] = None,
           detail: str = "", env=None, transport: Transport = _urllib_transport) -> bool:
    """Отправляет событие. Возвращает, дошло ли. Не поднимает исключений.

    Ошибку логируем предупреждением, а не роняем воркер: сосед мог быть
    недоступен, а ревью в PR уже лежит и от нашего доклада не зависит.
    """
    env = env if env is not None else os.environ
    if not configured(env):
        return False

    url = env["ISSUE_AGENT_URL"].strip().rstrip("/") + "/agent-event"
    body = json.dumps(build_payload(repo, pr_number, status,
                                    root_issue=root_issue, detail=detail)).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Agent-Signature-256": sign(env["AGENT_EVENT_SECRET"].strip(), body),
        "User-Agent": "pr-agent-reliability",
    }
    try:
        code, resp = transport("POST", url, body, headers)
    except Exception as exc:
        logger.warning("доклад в цикл Issue не ушёл (%s#%s, %s): %s",
                       repo, pr_number, status, exc)
        return False
    if code >= 300:
        logger.warning("цикл Issue отклонил доклад (%s#%s, %s): %s %r",
                       repo, pr_number, status, code, resp[:200])
        return False
    return True
