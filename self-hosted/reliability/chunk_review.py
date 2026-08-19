"""Ревью одного чанка прямым вызовом модели (ФТ-APRP-7 / deep-tier GLM-5).

pr-agent ревьюит ВЕСЬ PR — для чанка (подмножество файлов) он не подходит, поэтому
чанк ревьюится ПРЯМЫМ вызовом модели по патчам его файлов. Промпт — чистая функция
(тестируемо); реальный вызов модели инъектируется (seam `model_call`), дефолтный
адаптер (litellm → GLM-5) — под `pragma: no cover` (сеть).

Сбой/таймаут модели ПРОБРАСЫВАЕТСЯ (не глушим) — воркер/очередь ретраят чанк как
отдельную задачу; после исчерпания попыток чанк помечается неуспешным (ok=False) и
попадает в partial-reduce с явной пометкой (НФТ-APRP-6).

⚠️ ПРОМПТ — ЧЕРНОВОЙ, зеркалит инструкции ревью из `.pr_agent.toml`. Калибровать по
методу оценки качества (Q8) ДО активации в проде (пункт B). Качество — главный
критерий (Q4).
"""
from __future__ import annotations

import os

# Зеркалит [pr_reviewer].extra_instructions из .pr_agent.toml (единый стиль ревью).
DEFAULT_REVIEW_INSTRUCTIONS = (
    "Reply in English. Focus on correctness, security (no secret/PII leaks) and "
    "clear defects introduced by the diff. Don't nitpick formatting."
)

SYSTEM_PROMPT = (
    "You are a senior code reviewer. You are given patches for a SUBSET of files from "
    "a larger pull request. Review ONLY the shown patches. Report concrete defects "
    "introduced by the diff: correctness bugs, security issues, error/resource "
    "handling. Be specific: name the file and explain what is wrong and why. If a "
    "patch looks correct, say so briefly. Do NOT invent issues and do NOT comment on "
    "files that are not shown. Output concise GitHub-flavored Markdown."
)


def build_review_prompt(files_with_patches: list, *,
                        extra_instructions: str = DEFAULT_REVIEW_INSTRUCTIONS) -> tuple:
    """(system, user) для вызова модели. `files_with_patches` — [(path, patch)].

    Инструкции — в SYSTEM (не в user): в user только сам дифф, чтобы слова из
    инструкций (напр. 'secret', 'leaks') не «протекали» в область ревьюируемого
    кода и не порождали ложных находок."""
    blocks = [f"### `{path}`\n```diff\n{patch}\n```"
              for path, patch in files_with_patches if patch]
    system = f"{SYSTEM_PROMPT}\n\n{extra_instructions}"
    user = "Review these changes:\n\n" + "\n\n".join(blocks)
    return system, user


def review_chunk(model_call, files_with_patches: list, *,
                 extra_instructions: str = DEFAULT_REVIEW_INSTRUCTIONS) -> str:
    """Отревьюить чанк: собрать промпт, вызвать модель, вернуть findings (Markdown).

    `model_call(system, user) -> str` инъектируется. Пустой чанк (только бинарные/
    пустые патчи) → короткая пометка, без вызова модели. Исключение модели
    ПРОБРАСЫВАЕТСЯ (ретрай/DLQ на очереди)."""
    reviewable = [(p, patch) for p, patch in files_with_patches if patch]
    if not reviewable:
        return "_Нет текстовых изменений для ревью (бинарные/пустые патчи)._"
    system, user = build_review_prompt(reviewable, extra_instructions=extra_instructions)
    return (model_call(system, user) or "").strip()


def patches_for_files(client, repo: str, number: int, files: list) -> list:
    """[(path, patch)] для файлов чанка — берём патчи из list_pull_files (ФТ-APRP-7)."""
    by_name = {f["filename"]: f.get("patch", "") for f in client.list_pull_files(repo, number)}
    return [(p, by_name.get(p, "")) for p in files]


def glm_model_call(system: str, user: str) -> str:  # pragma: no cover - сеть/litellm
    """Реальный вызов deep-tier модели (GLM-5) через litellm (есть в образе pr-agent).

    Эндпоинт/ключ/модель — из тех же env, что и pr-agent (OPENAI_API_BASE, OPENAI_KEY,
    DEEP_MODEL|CONFIG_MODEL). Таймаут — CONFIG_AI_TIMEOUT (внутренний слой вложенности,
    ФТ-APRP-11)."""
    from litellm import completion
    model = os.environ.get("DEEP_MODEL") or os.environ.get("CONFIG_MODEL") or ""
    resp = completion(
        model=model,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        api_base=os.environ.get("OPENAI_API_BASE") or None,
        api_key=os.environ.get("OPENAI_KEY") or None,
        timeout=float(os.environ.get("CONFIG_AI_TIMEOUT", "60")),
    )
    return resp.choices[0].message.content or ""
