"""Планировщик чанков + приоритизация/бюджет (ФТ-APRP-4 / ФТ-APRP-5).

Принцип PO (Q6): **эффективный расход ресурсов** — усилие ревью пропорционально
важности изменений. Поэтому:
1. исключаем сгенерённое/вендорное (ревьюить незачем — экономия);
2. ранжируем по важности (core > tests > config > docs);
3. пакуем в чанки ≤ токен-бюджета (каждый уложится в таймаут → качество, ФТ-APRP-7);
4. при общем бюджете — overflow отбрасывает НИЗШИЙ приоритет с явной пометкой
   (ФТ-APRP-5, «не молчать» по-чанково — НФТ-APRP-6).

Чистые функции, вход — список FileChange из sizing. Правила исключений/приоритета —
конфигурируемы (дефолты ниже); детальная проработка правил — followup БФТ.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field

from reliability.sizing import FileChange, estimate_tokens

# Сгенерённое/вендорное — не ревьюим (подстроки пути, регистронезависимо).
DEFAULT_EXCLUDE = (
    "node_modules/", "vendor/", "third_party/", ".venv/", "dist/", "build/",
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "cargo.lock",
    "go.sum", ".min.js", ".min.css", "_pb2.py", ".pb.go", ".generated.", "/generated/",
    ".snap", ".lock",
)


class Priority(enum.IntEnum):
    CORE = 0     # исходный код — важнее всего
    TESTS = 1
    CONFIG = 2
    DOCS = 3     # документация — ниже всего


@dataclass(frozen=True)
class Chunk:
    index: int
    files: tuple  # tuple[FileChange]
    est_tokens: int
    oversized: bool = False  # один файл сам по себе > бюджета чанка


@dataclass
class ChunkPlan:
    chunks: list = field(default_factory=list)
    excluded: list = field(default_factory=list)          # сгенерённое/вендорное
    overflow_skipped: list = field(default_factory=list)  # не влезло в общий бюджет


def is_excluded(path: str, patterns=DEFAULT_EXCLUDE) -> bool:
    p = path.lower()
    return any(pat in p for pat in patterns)


def file_priority(path: str) -> Priority:
    p = path.lower()
    base = p.rsplit("/", 1)[-1]
    # тесты
    if ("test" in p and ("/test" in p or base.startswith("test") or "_test." in base
                          or ".test." in base or ".spec." in base)) or "/tests/" in p:
        return Priority.TESTS
    # документация
    if p.endswith((".md", ".rst", ".txt", ".adoc")) or "/docs/" in p or base in (
            "license", "readme"):
        return Priority.DOCS
    # конфиги
    if p.endswith((".toml", ".yaml", ".yml", ".json", ".ini", ".cfg", ".conf", ".env")) \
            or base in ("dockerfile", "makefile") or "/.github/" in p:
        return Priority.CONFIG
    return Priority.CORE


def plan_chunks(files: list[FileChange], *, chunk_budget_tokens: int,
                total_budget_tokens: int = 0, exclude=DEFAULT_EXCLUDE) -> ChunkPlan:
    """Разложить изменённые файлы в приоритизированные чанки под бюджет.

    chunk_budget_tokens — потолок одного чанка (≈ токен-бюджет модели, sizing).
    total_budget_tokens=0 — общий бюджет не ограничен (ревьюим всё после исключений)."""
    plan = ChunkPlan()
    reviewable: list[FileChange] = []
    for f in files:
        if is_excluded(f.path, exclude):
            plan.excluded.append(f.path)
        else:
            reviewable.append(f)

    # стабильная сортировка: приоритет ↑, затем крупные раньше (быстрее закрыть важное),
    # затем путь (детерминизм для тестов и кэша)
    reviewable.sort(key=lambda f: (file_priority(f.path),
                                   -(f.additions + f.deletions), f.path))

    idx = 0
    spent = 0
    cur: list[FileChange] = []
    cur_tokens = 0

    def flush(oversized=False):
        nonlocal idx, cur, cur_tokens
        if cur:
            plan.chunks.append(Chunk(idx, tuple(cur), cur_tokens, oversized))
            idx += 1
            cur = []
            cur_tokens = 0

    for f in reviewable:
        ftok = estimate_tokens(f.additions, f.deletions)
        # общий бюджет исчерпан → остаток (низший приоритет) в overflow
        if total_budget_tokens and spent + ftok > total_budget_tokens and (cur or plan.chunks):
            plan.overflow_skipped.append(f.path)
            continue
        if ftok > chunk_budget_tokens:
            flush()                       # закрыть текущий
            plan.chunks.append(Chunk(idx, (f,), ftok, oversized=True))  # отдельный «крупный»
            idx += 1
            spent += ftok
            continue
        if cur_tokens + ftok > chunk_budget_tokens:
            flush()
        cur.append(f)
        cur_tokens += ftok
        spent += ftok
    flush()
    return plan
