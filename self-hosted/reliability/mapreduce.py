"""Оркестрация адаптивной обработки PR (ФТ-APRP-2/6/8).

Связывает воедино: классификацию размера (sizing) → план чанков (chunking) →
fan-in job-стор (state) → синтез (reduce). Control-flow — чистый/сторовый; внешние
операции (list_pull_files, review_chunk, publish) инъектируются на уровне воркера.

⚠️ Модуль НЕ подключён к живому воркеру. Активация (маршрутизация large-PR через
map-reduce вместо одиночного прохода) — отдельный осознанный шаг, как переключение
entrypoint: до него прод-поведение не меняется. Требует решения по адаптеру ревью
чанка (прямой вызов модели + промпт) и калибровке порога на модель (Q2/Q8).
"""
from __future__ import annotations

from reliability.chunking import ChunkPlan, plan_chunks
from reliability.reduce import ChunkResult
from reliability.sizing import DiffWeight, FileChange, SizeClass, classify, diff_weight

CHUNK_EVENT = "chunk"     # event_type подзадачи-чанка (fan-out)
REDUCE_EVENT = "reduce"   # event_type сборки (fan-in)


def job_key_for(repo: str, number: int, head_sha: str) -> str:
    """Ключ job = стабильный идентификатор большого PR на конкретный head_sha."""
    return f"{repo}#{number}@{head_sha}"


def route(files: list[FileChange], *, chunk_budget_tokens: int,
          total_budget_tokens: int = 0) -> tuple:
    """Классифицировать PR и (для large) построить план чанков.

    small — если весь дифф укладывается в бюджет одного вызова (chunk_budget);
    тогда идёт обычный одиночный проход (план не нужен). Возвращает
    (SizeClass, DiffWeight, ChunkPlan|None)."""
    w = diff_weight(files)
    sc = classify(w, large_threshold_tokens=chunk_budget_tokens)
    if sc == SizeClass.SMALL:
        return sc, w, None
    plan = plan_chunks(files, chunk_budget_tokens=chunk_budget_tokens,
                       total_budget_tokens=total_budget_tokens)
    return sc, w, plan


def build_chunk_payloads(repo: str, number: int, head_sha: str,
                         job_key: str, plan: ChunkPlan) -> list:
    """Payload'ы событий-чанков для fan-out (по одному на чанк плана)."""
    return [
        {"event_type": CHUNK_EVENT, "repo": repo, "number": number, "head_sha": head_sha,
         "job_key": job_key, "chunk_index": c.index,
         "files": [f.path for f in c.files]}
        for c in plan.chunks
    ]


def claim_reduce(store, job_key: str) -> bool:
    """Fan-in триггер: все чанки отчитались И этот вызов выиграл CAS-барьер (M4).
    True — ровно у одного (он и запускает reduce); иначе False (ждём/пропускаем)."""
    if not store.job_all_reported(job_key):
        return False
    return store.try_start_reduce(job_key)


def collect_results(store, job_key: str) -> list:
    """Собрать findings чанков из стора в ChunkResult для синтеза (reduce)."""
    return [ChunkResult(index=idx, files=tuple(files), findings=findings, ok=ok)
            for idx, files, findings, ok in store.job_findings(job_key)]
