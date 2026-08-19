"""Классификация размера PR (ФТ-APRP-1 / НФТ-APRP-7).

Дёшево, без LLM, оценивает «вес» диффа и относит PR к `small`/`large`. Порог —
не фикс-число, а правило (Q2): выводится из контекстного окна активной модели и
бюджета таймаута. Здесь — чистые функции (вход: список изменённых файлов), сам
источник данных (GitHub API list_pull_files) инъектируется на уровне адаптера.

Правило веса: оценка токенов ≈ (added+deleted строк) × TOKENS_PER_LINE + overhead
на файл. Это грубая, но монотонная оценка — для маршрутизации достаточно; точная
токенизация не нужна (и зависит от модели).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass

# Грубая оценка: строка кода ≈ ~12 токенов (тюнится); overhead на файл — хедер диффа.
TOKENS_PER_LINE = 12
TOKENS_PER_FILE_OVERHEAD = 40


class SizeClass(str, enum.Enum):
    SMALL = "small"   # укладывается в один проход модели в пределах таймаута
    LARGE = "large"   # heavy-path: map-reduce по чанкам


@dataclass(frozen=True)
class FileChange:
    path: str
    additions: int
    deletions: int
    status: str = "modified"  # added|modified|removed|renamed


@dataclass(frozen=True)
class DiffWeight:
    files: int
    lines: int          # added + deleted
    est_tokens: int


def files_from_api(raw: list) -> list:
    """Маппинг ответа github_client.list_pull_files → список FileChange."""
    return [FileChange(r.get("filename", ""), int(r.get("additions", 0)),
                       int(r.get("deletions", 0)), r.get("status", "modified"))
            for r in raw]


def estimate_tokens(additions: int, deletions: int) -> int:
    """Оценка токенов одного файла по числу изменённых строк (+overhead хедера)."""
    return (max(0, additions) + max(0, deletions)) * TOKENS_PER_LINE + TOKENS_PER_FILE_OVERHEAD


def diff_weight(files: list[FileChange]) -> DiffWeight:
    """Суммарный вес диффа по списку изменённых файлов."""
    lines = sum(max(0, f.additions) + max(0, f.deletions) for f in files)
    est = sum(estimate_tokens(f.additions, f.deletions) for f in files)
    return DiffWeight(files=len(files), lines=lines, est_tokens=est)


def model_token_budget(context_window: int, *, safe_frac: float = 0.5,
                       reserve_output: int = 4000) -> int:
    """Правило Q2: сколько токенов промпта безопасно уложить для активной модели.

    Берём безопасную долю контекстного окна за вычетом резерва на ответ модели.
    Это верхняя граница одного вызова (порог small/large и бюджет чанка). Точное
    число калибруется на модель замером латентности vs размер (НФТ-APRP-2б)."""
    return max(0, int(context_window * safe_frac) - reserve_output)


def classify(weight: DiffWeight, *, large_threshold_tokens: int,
             max_files: int = 0) -> SizeClass:
    """`large`, если вес превышает токен-бюджет модели (или лимит файлов, если задан).

    `large_threshold_tokens` — обычно `model_token_budget(...)`. `max_files=0` —
    файловый лимит отключён (только по токенам)."""
    if weight.est_tokens > large_threshold_tokens:
        return SizeClass.LARGE
    if max_files and weight.files > max_files:
        return SizeClass.LARGE
    return SizeClass.SMALL
