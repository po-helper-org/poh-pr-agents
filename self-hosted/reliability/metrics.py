"""Счётчики + экспозиция метрик (СТ-27б, 33..35).

Потокобезопасно. `render_prometheus` отдаёт снапшот в текстовом формате
Prometheus (`/metrics` в app.py) — это наблюдаемость К-5: dead_letter_total,
gateway_*, глубина очереди видны снаружи, «тихих» провалов не остаётся.
"""
from __future__ import annotations

import threading
from typing import Optional

_counters: dict[str, int] = {}
_lock = threading.Lock()

_PREFIX = "reliability_"


def render_prometheus(gauges: Optional[dict] = None) -> str:
    """Снапшот счётчиков (+ опциональные gauge, напр. queue_depth) в формате
    Prometheus text exposition. Имена префиксуются `reliability_`; каждая метрика
    сопровождается `# TYPE`. Значения — целые/числа, метки не используются (плоско)."""
    lines: list[str] = []
    for name, value in sorted(snapshot().items()):
        metric = f"{_PREFIX}{name}"
        lines.append(f"# TYPE {metric} counter")
        lines.append(f"{metric} {value}")
    for name, value in sorted((gauges or {}).items()):
        metric = f"{_PREFIX}{name}"
        lines.append(f"# TYPE {metric} gauge")
        lines.append(f"{metric} {value}")
    return "\n".join(lines) + "\n"


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
