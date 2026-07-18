"""FastAPI ingress — приём webhook и постановка в durable queue (СТ-1..5, Фаза 2).

Тонкий слой: подпись/dedup/парсинг — в `ingress`, затем событие кладётся в очередь.
Обработку ведёт отдельный процесс-воркер (`reliability.worker.main`), разделяющий
те же SQLite-файлы (state + queue). Разворачивается вместо webhook-входа pr-agent.
"""
from __future__ import annotations

import os

from fastapi import FastAPI, Request, Response

from reliability import logging_setup, metrics
from reliability.ingress import handle_webhook
from reliability.queue import DurableQueue
from reliability.state import StateStore, event_to_dict
from reliability.webhook import enrich_events

logging_setup.configure()  # reliability.* → stdout (логи webhook'ов в контейнере ingress)

WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
STORE_PATH = os.environ.get("RELIABILITY_DB", "/data/reliability.db")
QUEUE_PATH = os.environ.get("RELIABILITY_QUEUE", "/data/queue.db")

app = FastAPI()
_store = StateStore(STORE_PATH)
_queue = DurableQueue(QUEUE_PATH)


def _enrich(events):  # pragma: no cover - реальный GitHub-порт, проверяется на смоуке
    from reliability import analyze_adapter
    from reliability.github_client import GitHubAppClient
    client = GitHubAppClient(token_provider=analyze_adapter.installation_token)
    return enrich_events(events, client.get_pull_head_sha)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/metrics")
def metrics_endpoint():  # pragma: no cover - тонкая обвязка над render_prometheus
    # gauge'и снимаем в момент запроса; глубина очереди и размер DLQ — сигнал К-5
    # (растёт DLQ → есть непрокрученные провалы) и вход для автоскейла (СТ-18).
    gauges = {"queue_depth": _queue.depth(), "dead_letters": len(_queue.dead_letters())}
    return Response(content=metrics.render_prometheus(gauges), media_type="text/plain")


# Принимаем и новый `/webhook`, и легаси-путь pr-agent `/api/v1/github_webhooks`:
# существующие GitHub App'ы (и register-app.html) настроены на легаси-путь, поэтому
# go-live НЕ требует менять webhook-URL App'а — переключение прода бесшовно и легко
# откатывается. Оба пути ведут в один обработчик.
@app.post("/webhook")
@app.post("/api/v1/github_webhooks")
async def webhook(request: Request):
    raw = await request.body()

    def schedule(event):
        _queue.enqueue(event_to_dict(event), event.repo)  # партиция = repo (СТ-7)

    status = handle_webhook(raw, dict(request.headers),
                            secret=WEBHOOK_SECRET, store=_store, schedule=schedule,
                            enrich=_enrich)
    if status != 200:
        return Response(status_code=status)
    return {}  # СТ-4: быстрый 200, работа — в очереди/воркере
