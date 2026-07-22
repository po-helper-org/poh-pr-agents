"""Синтез единого ревью из результатов чанков (ФТ-APRP-8 / НФТ-APRP-6).

Reduce собирает findings готовых чанков в один коммент и **явно перечисляет
непройденное** (сбойные чанки + overflow) — «не молчать» по-чанково (НФТ-APRP-6).
Здесь — структурная сборка (детерминированная, без LLM). Опциональная склейка
дешёвой моделью (дедуп/переписывание) — инъектируемый seam `glue` (cheap-tier,
ФТ-APRP-10), по умолчанию identity.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

REVIEW_MARKER = "<!-- reliability:review -->"  # один на PR (СТ-25)


@dataclass(frozen=True)
class ChunkResult:
    index: int
    files: tuple          # tuple[str] — пути файлов чанка
    findings: str         # текст ревью от модели (может быть пустым)
    ok: bool = True       # False — чанк не отревьюён (сбой/таймаут)


def synthesize(results: list[ChunkResult], *, failed_files=None, overflow_files=None,
               glue: Optional[Callable[[str], str]] = None) -> str:
    """Единое тело ревью. `glue` — опц. переписывание дешёвой моделью (по умолч. как есть)."""
    failed_files = list(failed_files or [])
    overflow_files = list(overflow_files or [])
    ok = [r for r in results if r.ok and r.findings.strip()]
    not_reviewed = [f for r in results if not r.ok for f in r.files]

    parts = [f"## 🤖 Ревью по частям ({len(ok)}/{len(results)} чанков готово)", ""]
    if ok:
        body = "\n\n".join(f"### Файлы: {', '.join(r.files)}\n{r.findings.strip()}" for r in ok)
        parts.append(glue(body) if glue else body)
    else:
        parts.append("_Содержательных находок нет либо ни один чанк не завершился._")

    missing = not_reviewed + failed_files + overflow_files
    if missing:
        parts += ["", "### ⚠️ Не отревьюено (требует внимания)"]
        for f in not_reviewed + failed_files:
            parts.append(f"- `{f}` — не удалось отревьюить (сбой/таймаут), повтор автоматически")
        for f in overflow_files:
            parts.append(f"- `{f}` — вне бюджета этого прохода")

    parts += ["", REVIEW_MARKER]
    return "\n".join(parts)


def publish_review(client, repo: str, number: int, results, **kw) -> str:
    """Идемпотентно опубликовать итог (СТ-25). Единственный писатель коммента (M3)."""
    body = synthesize(results, **kw)
    client.upsert_comment(repo, number, REVIEW_MARKER, body)
    return body
