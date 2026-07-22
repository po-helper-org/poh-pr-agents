"""Первичная обратная связь для больших PR (ФТ-APRP-3 / БТ-APRP-2 / НФТ-APRP-1).

Как только PR классифицирован `large`, публикуем ОДИН идемпотентный коммент
(СТ-25) «PR большой, ревью по частям, ETA ~T» — контракт «не молчать» с нулевой
секунды (SLA ≤ 60 с). Текст — шаблон (LLM не нужен), публикатор инъектируется.
"""
from __future__ import annotations

import math

from reliability.chunking import ChunkPlan
from reliability.sizing import DiffWeight

ACK_MARKER = "<!-- reliability:ack -->"  # один на PR: upsert правит, не плодит

# Грубая оценка ETA: чанки идут параллельно (в пределах rate-limit gateway).
DEFAULT_CONCURRENCY = 3
DEFAULT_PER_CHUNK_SEC = 40
SLA_LARGE_REVIEW_SEC = 600  # КУ-2: ≤ 10 мин


def estimate_eta_seconds(num_chunks: int, *, concurrency: int = DEFAULT_CONCURRENCY,
                         per_chunk_sec: int = DEFAULT_PER_CHUNK_SEC) -> int:
    """~ волн(чанки/параллелизм) × время чанка, но не обещаем больше SLA (КУ-2)."""
    if num_chunks <= 0:
        return 0
    waves = math.ceil(num_chunks / max(1, concurrency))
    return min(waves * per_chunk_sec, SLA_LARGE_REVIEW_SEC)


def _fmt_eta(seconds: int) -> str:
    if seconds < 60:
        return f"~{seconds} с"
    return f"~{math.ceil(seconds / 60)} мин"


def build_ack_comment(weight: DiffWeight, plan: ChunkPlan, *,
                      concurrency: int = DEFAULT_CONCURRENCY,
                      per_chunk_sec: int = DEFAULT_PER_CHUNK_SEC) -> str:
    n = len(plan.chunks)
    eta = estimate_eta_seconds(n, concurrency=concurrency, per_chunk_sec=per_chunk_sec)
    lines = [
        "🔎 **Большой PR — ревью выполняется по частям.**",
        "",
        f"- Файлов к ревью: **{weight.files}** (~{weight.lines} изменённых строк)",
        f"- Частей (чанков): **{n}**, ориентировочно **{_fmt_eta(eta)}**",
    ]
    if plan.excluded:
        lines.append(f"- Пропущено как сгенерённое/вендорное: {len(plan.excluded)}")
    if plan.overflow_skipped:
        lines.append(f"- Вне бюджета этого прохода: {len(plan.overflow_skipped)} "
                     "(будут помечены в итоге)")
    lines += ["", "_Прогресс и итог появятся здесь же по мере готовности._", "", ACK_MARKER]
    return "\n".join(lines)


def publish_ack(client, repo: str, number: int, weight: DiffWeight, plan: ChunkPlan,
                **kw) -> str:
    """Идемпотентно опубликовать fast-ack (СТ-25). Возвращает тело коммента."""
    body = build_ack_comment(weight, plan, **kw)
    client.upsert_comment(repo, number, ACK_MARKER, body)
    return body


def build_progress_comment(weight: DiffWeight, plan: ChunkPlan, *, done: int,
                           failed: int = 0) -> str:
    """Инкрементальный прогресс (ФТ-APRP-9): правит тот же ack-коммент по маркеру."""
    total = len(plan.chunks)
    lines = [
        "🔎 **Большой PR — ревью по частям.**",
        "",
        f"- Прогресс: **{done}/{total}** чанков готово"
        + (f" (не удалось: {failed})" if failed else ""),
        f"- Файлов к ревью: {weight.files}",
    ]
    if done >= total:
        lines.append("- Готово — собираю итог…")
    lines += ["", ACK_MARKER]
    return "\n".join(lines)


def publish_progress(client, repo: str, number: int, weight: DiffWeight,
                     plan: ChunkPlan, *, done: int, failed: int = 0) -> str:
    """Идемпотентно обновить прогресс на ack-комменте (единый писатель, M3/СТ-25)."""
    body = build_progress_comment(weight, plan, done=done, failed=failed)
    client.upsert_comment(repo, number, ACK_MARKER, body)
    return body


def build_progress_counts(done: int, total: int, failed: int = 0) -> str:
    """Прогресс только по счётчикам job (для воркера — без DiffWeight/ChunkPlan)."""
    lines = [
        "🔎 **Большой PR — ревью по частям.**",
        "",
        f"- Прогресс: **{done}/{total}** чанков готово"
        + (f" (не удалось: {failed})" if failed else ""),
    ]
    if done >= total:
        lines.append("- Готово — собираю итог…")
    lines += ["", ACK_MARKER]
    return "\n".join(lines)


def publish_progress_counts(client, repo: str, number: int, *, done: int, total: int,
                            failed: int = 0) -> str:
    body = build_progress_counts(done, total, failed)
    client.upsert_comment(repo, number, ACK_MARKER, body)
    return body
