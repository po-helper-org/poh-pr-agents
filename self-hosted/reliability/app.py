"""FastAPI ingress — приём webhook и постановка в durable queue (СТ-1..5, Фаза 2).

Тонкий слой: подпись/dedup/парсинг — в `ingress`, затем событие кладётся в очередь.
Обработку ведёт отдельный процесс-воркер (`reliability.worker.main`), разделяющий
те же SQLite-файлы (state + queue). Разворачивается вместо webhook-входа pr-agent.
"""
from __future__ import annotations

import os

from fastapi import FastAPI, Request, Response

from reliability.ingress import handle_webhook
from reliability.queue import DurableQueue
from reliability.state import StateStore, event_to_dict

WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
STORE_PATH = os.environ.get("RELIABILITY_DB", "/data/reliability.db")
QUEUE_PATH = os.environ.get("RELIABILITY_QUEUE", "/data/queue.db")

app = FastAPI()
_store = StateStore(STORE_PATH)
_queue = DurableQueue(QUEUE_PATH)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request):
    raw = await request.body()

    def schedule(event):
        _queue.enqueue(event_to_dict(event), event.repo)  # партиция = repo (СТ-7)

    status = handle_webhook(raw, dict(request.headers),
                            secret=WEBHOOK_SECRET, store=_store, schedule=schedule)
    if status != 200:
        return Response(status_code=status)
    return {}  # СТ-4: быстрый 200, работа — в очереди/воркере
