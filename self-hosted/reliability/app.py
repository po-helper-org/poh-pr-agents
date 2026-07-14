"""FastAPI ingress — reliability-обвязка на живом webhook (СТ-1..5, 27).

Тонкий слой: вся логика — в протестированных модулях reliability.*. Запускается
в контейнере (fastapi/uvicorn уже есть у pr-agent). End-to-end заработает после
реализации `analyze_adapter` (следующий срез).
"""
from __future__ import annotations

import os

from fastapi import BackgroundTasks, FastAPI, Request, Response

from reliability import analyze_adapter
from reliability.github_client import GitHubAppClient
from reliability.ingress import handle_webhook
from reliability.state import StateStore
from reliability.supervisor import process

WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
STORE_PATH = os.environ.get("RELIABILITY_DB", "/data/reliability.db")
MAX_ATTEMPTS = int(os.environ.get("RELIABILITY_MAX_ATTEMPTS", "5"))

app = FastAPI()
_store = StateStore(STORE_PATH)
_client = GitHubAppClient(token_provider=analyze_adapter.installation_token)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request, bg: BackgroundTasks):
    raw = await request.body()

    def schedule(event):
        bg.add_task(process, event, analyze_adapter.run, _store, _client,
                    max_attempts=MAX_ATTEMPTS)

    status = handle_webhook(raw, dict(request.headers),
                            secret=WEBHOOK_SECRET, store=_store, schedule=schedule)
    if status != 200:
        return Response(status_code=status)
    return {}  # СТ-4: быстрый 200, работа в фоне
