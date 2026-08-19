"""Отправка ошибок и ключевых сбоев в Sentry (парно к logging_setup).

Зачем: логи в stdout Dokploy живут в контейнере и никого не будят. Главные
классы сбоя этого стека — не падение процесса, а *пойманный* сбой: задача ушла
в dead-letter (worker), пул провайдеров недоступен (gateway). Оба видны только
как строка лога. Sentry делает из них адресуемое событие с тегами repo/pr.

`configure()` идемпотентна и НЕОБЯЗАТЕЛЬНА: без SENTRY_DSN — no-op, стек ведёт
себя ровно как до интеграции (важно для локального запуска, тестов и отката —
достаточно убрать переменную из env).

⚠️ Скраббер (`_scrub_event`): в кадры стека sentry-sdk кладёт значения локальных
переменных, а по этому коду ходят installation-токен GitHub, GITHUB_PRIVATE_KEY_B64,
OPENAI_KEY и диффы приватных PR. Всё это ушло бы на серверы sentry.io. Денилист
имён вырезает значения ДО отправки — трогать его без нужды нельзя.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

_configured = False

# Ключи, значения которых не должны покидать периметр. Сопоставляется по
# ПОДСТРОКЕ имени в нижнем регистре: "github_private_key_b64" ловится по "key",
# "X-Hub-Signature" — по "signature".
_SECRET_KEY = re.compile(
    r"token|key|secret|password|passwd|private|authorization|auth|cookie|"
    r"signature|dsn|credential|diff|patch",
    re.IGNORECASE,
)
_FILTERED = "[Filtered]"
_MAX_VALUE_LEN = 2048  # длинные значения (диффы, тела ответов) режем, а не шлём целиком


def _scrub_mapping(d) -> None:
    """Заменить значения секретных ключей на [Filtered], длинные — обрезать. In-place."""
    if not isinstance(d, dict):
        return
    for k, v in list(d.items()):
        if isinstance(k, str) and _SECRET_KEY.search(k):
            d[k] = _FILTERED
        elif isinstance(v, dict):
            _scrub_mapping(v)
        elif isinstance(v, str) and len(v) > _MAX_VALUE_LEN:
            d[k] = v[:_MAX_VALUE_LEN] + "…[truncated]"


# Предсмертные жалобы aiohttp на незакрытые ресурсы. Приходят от логгера
# asyncio уровнем ERROR, без стека и без единого нашего кадра: сигнала в них
# ноль, а объём — 200 событий за месяц (PR-AGENT-9, PR-AGENT-T, PR-AGENT-X),
# в которых тонут настоящие сбои.
#
# Источник чужой и починке с нашей стороны не поддаётся: `pr_agent` заводит
# ClientSession внутри `asyncio.run(PRAgent().handle_request(...))`
# (analyze_adapter._real_invoke) и не закрывает её перед закрытием цикла. Наш
# код aiohttp не использует вовсе — поэтому отсев здесь не может спрятать
# ничего своего.
_AIOHTTP_LEAK_NOISE = ("Unclosed client session", "Unclosed connector")


def _is_aiohttp_leak_noise(event: dict) -> bool:
    if event.get("logger") != "asyncio":
        return False
    message = event.get("logentry", {}).get("message") or ""
    if not message:
        message = str((event.get("message") or ""))
    return message.startswith(_AIOHTTP_LEAK_NOISE)


# Отказы чужой стороны: провайдер модели не ответил или ответил 5xx. Контур
# повлиять на них не может, а выглядят они как обычные ошибки уровня error и
# заводят новое issue на каждую формулировку сообщения провайдера.
#
# Не отбрасываем — аутейдж провайдера остаётся причиной большей части
# несостоявшихся ревью, и слепота тут дороже шума. Понижаем до warning и
# склеиваем по типу, чтобы чужое было отличимо от своего с одного взгляда.
# Ответы GitHub (403/422/502) сюда НЕ входят: за ними обычно стоит наша
# ошибка — нет прав, кривой запрос, — и понижать их нельзя.
_EXTERNAL_FAILURES = {
    "GatewayUnavailable",   # все провайдеры шлюза отказали (gateway.py)
    "RateLimitError",       # 429 от провайдера модели
    "APITimeoutError",
    "APIConnectionError",
    "InternalServerError",  # 5xx и 524
}


def _classify_external(event: dict) -> None:
    values = (event.get("exception") or {}).get("values") or []
    if not values:
        return
    exc_type = values[-1].get("type")
    if exc_type in _EXTERNAL_FAILURES:
        event["level"] = "warning"
        event["fingerprint"] = ["external_failure", exc_type]
        event.setdefault("tags", {})["failure_side"] = "external"


def _scrub_event(event: dict, hint=None) -> Optional[dict]:
    """before_send: отсеять шум чужой библиотеки, вычистить секреты."""
    if _is_aiohttp_leak_noise(event):
        return None
    _classify_external(event)
    for value in (event.get("exception") or {}).get("values") or []:
        for frame in (value.get("stacktrace") or {}).get("frames") or []:
            _scrub_mapping(frame.get("vars"))
    request = event.get("request")
    if isinstance(request, dict):
        _scrub_mapping(request.get("headers"))
        _scrub_mapping(request.get("cookies"))
        _scrub_mapping(request.get("env"))
        request.pop("data", None)  # тело webhook'а = payload GitHub, наружу не нужно
    _scrub_mapping(event.get("extra"))
    _scrub_mapping(event.get("contexts"))
    return event


def configure(service: str) -> bool:
    """Инициализировать Sentry для процесса `service` (ingress|worker|sweeper).

    Возвращает True, если Sentry включён. Без SENTRY_DSN — no-op → False.
    Идемпотентна: повторный вызов (реимпорт модуля) не плодит второй клиент.
    """
    global _configured
    if _configured:
        return True
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        return False
    try:
        import sentry_sdk
        from sentry_sdk.integrations.logging import LoggingIntegration
    except ImportError:  # pragma: no cover - в проде ставится в Dockerfile.reliability
        logger.warning("SENTRY_DSN задан, но sentry-sdk не установлен — Sentry выключен")
        return False

    sentry_sdk.init(
        dsn=dsn,
        environment=os.environ.get("SENTRY_ENVIRONMENT", "production"),
        release=os.environ.get("SENTRY_RELEASE") or None,
        # tracing выключен по умолчанию: длительности уже есть в /metrics,
        # а трассы на 100k событий/сутки съедят квоту без новой информации.
        traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0")),
        before_send=_scrub_event,
        integrations=[LoggingIntegration(level=logging.INFO,      # INFO → breadcrumb
                                         event_level=logging.ERROR)],  # ERROR → событие
    )
    sentry_sdk.set_tag("service", service)
    _configured = True
    logger.info("sentry enabled: service=%s environment=%s", service,
                os.environ.get("SENTRY_ENVIRONMENT", "production"))
    return True


def _event_tags(event) -> dict:
    return {
        "repo": event.repo,
        "pr": str(event.number),
        "command": event.command,
        "delivery_id": event.delivery_id,
        "event_type": event.event_type,
    }


def capture_dead_letter(event, reason: str, attempts: int) -> None:
    """Задача исчерпала попытки и ушла в DLQ (СТ-27) — эскалация в Sentry.

    fingerprint по (dead_letter, reason): аутейдж Z.AI даёт одно issue с сотней
    событий, а не сотню отдельных issue по одному на PR.
    """
    if not _configured:
        return
    import sentry_sdk

    with sentry_sdk.new_scope() as scope:
        for k, v in _event_tags(event).items():
            scope.set_tag(k, v)
        scope.set_tag("reason", reason)
        scope.set_extra("attempts", attempts)
        scope.fingerprint = ["dead_letter", reason]
        sentry_sdk.capture_message(
            f"dead-letter: {event.command} on {event.repo}#{event.number} ({reason})",
            level="error")


def capture_gateway_unavailable(event, errors) -> None:
    """Все LLM-провайдеры недоступны (СТ-19..22).

    Группируем по набору классов сбоя, а не по PR — иначе один аутейдж Z.AI
    разлетается на issue по числу открытых PR.
    """
    if not _configured:
        return
    import sentry_sdk

    classes = ",".join(sorted({cls for _, cls in errors})) or "unknown"
    with sentry_sdk.new_scope() as scope:
        if event is not None:
            for k, v in _event_tags(event).items():
                scope.set_tag(k, v)
        scope.set_tag("failure_classes", classes)
        scope.set_extra("providers", [{"provider": n, "error": c} for n, c in errors])
        scope.fingerprint = ["gateway_unavailable", classes]
        sentry_sdk.capture_message(
            f"gateway unavailable: all providers failed ({classes})", level="error")
