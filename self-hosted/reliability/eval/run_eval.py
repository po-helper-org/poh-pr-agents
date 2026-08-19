#!/usr/bin/env python3
"""Прогон оценки качества ревью «посевными багами» (способ A, Q8).

Гоняет набор патчей с известными дефектами через настоящий адаптер ревью чанка и
считает объективные метрики: recall (нашла X из Y посаженных багов) и false-positive
на чистом коде. Оценщику judge'ить код не нужно — на выходе числа.

Режимы:
  --live   реальный GLM-5 (chunk_review.glm_model_call) — единственный осмысленный
           замер качества; требует OPENAI_KEY/OPENAI_API_BASE/DEEP_MODEL в окружении.
  --demo   канонический фейк модели — только чтобы прогнать «трубу» харнесса и увидеть
           формат отчёта (НЕ измерение качества).

Запуск:
    cd self-hosted && python3 reliability/eval/run_eval.py --live
"""
from __future__ import annotations

import argparse
import os
import re
import sys

_SELF_HOSTED = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _SELF_HOSTED not in sys.path:
    sys.path.insert(0, _SELF_HOSTED)

from reliability.quality_eval import DEFAULT_CASES, evaluate


def demo_model(system: str, user: str) -> str:
    """Грубый фейк: «замечает» типовые паттерны в патче. Только для проверки харнесса,
    не для оценки качества (иначе мерили бы фейк, а не GLM-5)."""
    u = user.lower()
    hits = []
    if "select" in u and "+" in u:
        hits.append("Возможная SQL-инъекция: конкатенация ввода в запрос.")
    if ", _ :=" in user or ", _:=" in user:
        hits.append("Проигнорирована ошибка (err = _) — стоит проверить.")
    if "open(" in u and "with" not in u and "close" not in u:
        hits.append("Возможная утечка ресурса: файл не закрывается.")
    if re.search(r"(api_key|secret|sk-)", u):
        hits.append("Похоже на захардкоженный секрет/ключ.")
    if "len(" in u and "]" in u:
        hits.append("Возможный выход за границу массива (index out of range).")
    if "user." in u and "none" in u:
        hits.append("Возможное разыменование None — нет проверки.")
    return "\n".join(f"- {h}" for h in hits) if hits else "No issues found — код выглядит корректно."


def main():
    p = argparse.ArgumentParser(description="Оценка качества ревью (посевные баги)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--live", action="store_true", help="реальный GLM-5")
    g.add_argument("--demo", action="store_true", help="фейк-модель (проверка харнесса)")
    p.add_argument("--min-recall", type=float, default=0.7, help="порог recall для exit 0")
    p.add_argument("--max-fp", type=float, default=0.2, help="порог false-positive для exit 0")
    p.add_argument("--show-findings", action="store_true", help="печатать сами находки")
    args = p.parse_args()

    if args.live:
        from reliability.chunk_review import glm_model_call
        model = glm_model_call
        mode = "LIVE (GLM-5)"
    else:
        model = demo_model
        mode = "DEMO (фейк — НЕ измерение качества)"

    rep = evaluate(DEFAULT_CASES, model)

    print("=" * 68)
    print(f"Оценка качества ревью — {mode}")
    print("=" * 68)
    for r in rep.results:
        tag = "чисто" if r.clean else "баг"
        mark = "✅" if r.passed else "❌"
        print(f"  {mark} [{tag}] {r.name}")
        if args.show_findings:
            print(f"      → {r.findings.strip()[:200]}")
    print("-" * 68)
    print(f"  Recall (нашла посаженных багов): {rep.caught}/{len(rep.seeded)} = {rep.recall*100:.0f}%")
    print(f"  False positives (выдумала на чистом): {rep.false_positives}/{len(rep.clean_cases)} "
          f"= {rep.false_positive_rate*100:.0f}%")
    print("=" * 68)

    ok = rep.recall >= args.min_recall and rep.false_positive_rate <= args.max_fp
    if args.demo:
        print("DEMO-режим: числа отражают фейк, не GLM-5. Для реального замера — --live.")
    print("✅ ПОРОГ ПРОЙДЕН" if ok else "❌ НИЖЕ ПОРОГА")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
