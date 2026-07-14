"""Минимальные счётчики (СТ-27б, зерно под СТ-33).

Потокобезопасно; экспозиция в /metrics — отдельный срез. Полноценная
observability (queue_depth, latency p50/p95, success_rate, …) — Фазы 3–5.
"""
from __future__ import annotations

import threading

_counters: dict[str, int] = {}
_lock = threading.Lock()


def incr(name: str, n: int = 1) -> None:
    with _lock:
        _counters[name] = _counters.get(name, 0) + n


def get(name: str) -> int:
    with _lock:
        return _counters.get(name, 0)


def snapshot() -> dict[str, int]:
    with _lock:
        return dict(_counters)


def reset() -> None:
    with _lock:
        _counters.clear()
