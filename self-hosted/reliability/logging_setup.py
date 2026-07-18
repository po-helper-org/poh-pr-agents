"""Единая настройка логов reliability → stdout (видно в логах контейнера Dokploy).

configure() идемпотентна: вешает один stdout-handler на логгер `reliability`,
уровень INFO, propagate=False (чтобы не дублироваться в root/uvicorn). Дочерние
логгеры (`reliability.ingress`, `reliability.worker`, …) наследуют его.
"""
from __future__ import annotations

import logging
import sys


def configure(level: int = logging.INFO) -> None:
    root = logging.getLogger("reliability")
    if root.handlers:  # уже настроено (повторный импорт/вызов) — не плодим handler'ы
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
    root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False
