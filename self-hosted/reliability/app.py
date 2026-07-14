"""FastAPI ingress — reliability-обвязка на живом webhook (СТ-1..5, 27).

Тонкий слой: вся логика — в протестированных модулях reliability.*. Запускается
в контейнере (fastapi/uvicorn уже есть у pr-agent). End-to-end заработает после
реализации `analyze_adapter` (следующий срез).
"""
from __future__ import annotations

import json
import os

from fastapi import BackgroundTasks, FastAPI, Request, Response

from reliability import analyze_adapter
from reliability.github_client import GitHubAppClient
from reliability.security import verify_signature
from reliability.state import StateStore
from reliability.supervisor import process
from reliability.webhook import parse_events

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
    if not verify_signature(WEBHOOK_SECRET, raw, request.headers.get("X-Hub-Signature-256")):
        return Response(status_code=401)  # СТ-1
    delivery = request.headers.get("X-GitHub-Delivery", "")
    etype = request.headers.get("X-GitHub-Event", "")
    payload = json.loads(raw or b"{}")
    for event in parse_events(etype, delivery, payload):
        if _store.record_received(event):  # СТ-2 dedup
            bg.add_task(process, event, analyze_adapter.run, _store, _client,
                        max_attempts=MAX_ATTEMPTS)
    return {}  # СТ-4: быстрый 200, работа в фоне
