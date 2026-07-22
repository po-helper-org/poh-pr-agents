---
epic: APRP
title: Статус реализации APRP
synced: 2026-07-19
branch: feat/big-small-large-diff
---

# Статус реализации — APRP (адаптивная обработка PR по размеру)

Все компоненты — **аддитивно и покрыты тестами (210 unittest, stdlib-only, зелёные)**.
Прод-путь одиночного прохода **не тронут**: активация map-reduce — отдельный осознанный
шаг (как переключение entrypoint), потому что мерж в `main` = автодеплой.

## Готово (код + тесты)

| ФТ | Что | Модуль |
|---|---|---|
| ФТ-APRP-11 ✅ | Чистая вложенность таймаутов (ai<attempt<task<visibility), авто-исправление инверсии; visibility конфигурируем | `worker.py::resolve_worker_timeouts`, `docker-compose.yml` |
| ФТ-APRP-1 ✅ | Классификатор размера (diff_weight, classify small/large; порог из окна модели) | `sizing.py` |
| ФТ-APRP-4 ✅ | План чанков под токен-бюджет | `chunking.py::plan_chunks` |
| ФТ-APRP-5 ✅ | Приоритизация (core>tests>config>docs), исключение generated/vendored, overflow-пометка | `chunking.py` |
| ФТ-APRP-3 ✅ | Fast-ack коммент + ETA (SLA ≤60с) | `ack.py` |
| ФТ-APRP-9 ✅ | Инкрементальный прогресс N/M | `ack.py::build_progress_comment` |
| ФТ-APRP-8 ✅ | Reduce-синтез + явный список «не отревьюено» (partial) | `reduce.py` |
| ФТ-APRP-6 ✅ | Fan-in job-стор (findings в сторе M3, CAS-барьер reduce M4, идемпотентно) | `state.py` (jobs/chunk_findings), `mapreduce.py` |
| ФТ-APRP-10 ✅ | Тиринг моделей (cheap=GLM-4.7 / deep=GLM-5, data-driven) | `gateway.py::TieredGateway` |
| ФТ-APRP-7 ✅ | Ревью чанка прямым вызовом GLM-5 (промпт-чистая функция + seam модели) | `chunk_review.py` |
| — | Оркестрация (route/fan-out payloads/claim_reduce/collect) | `mapreduce.py` |
| — | Источник данных (файлы+патчи PR) | `github_client.py::list_pull_files` |

## Осталось — требует решения/активации

| # | Пункт | Почему не сделано автономно |
|---|---|---|
| A ✅ | Адаптер ревью чанка (`chunk_review.py`) — построен. **Промпт ЧЕРНОВОЙ** (зеркалит `.pr_agent.toml`), калибровать по Q8 | реализовано; промпт помечен на калибровку |
| B ✅ | **Активация в воркере** построена **за флагом `RELIABILITY_MAPREDUCE` (OFF по умолчанию)**: dispatch `chunk`/`reduce` (`worker.handle_lease`), маршрутизация большого `/review` в fan-out (`mapreduce_worker.route_and_fanout`), partial при DLQ чанка. Мерж в `main` безопасен — прод-путь не меняется, пока флаг выключен | реализовано; включение — осознанный шаг на стейджинге |
| C | **Калибровка порога/размера чанка на GLM-5/GLM-4.7** (Q2 числа: `RELIABILITY_CHUNK_BUDGET_TOKENS`) | Требует замера латентности vs размер на реальной модели |
| Q8 ✅ | **Метод оценки качества** — выбран способ A (посевные баги), харнесс построен (`reliability/eval/`, `quality_eval.py`). Метрики recall/false-positive, `--live` для GLM-5 | реализовано; расширять датасет реальными багами |
| ФТ-APRP-12 | Кэш ревью по blob-sha (опц.) | Followup (Q7) |

## Оценка качества (Q8 / способ A)

`reliability/eval/` — «посевные баги»: патчи с известными дефектами → прогон ревью →
объективные **recall** (нашла X из Y) и **false-positive** (выдумала на чистом коде).
Скоринг чистый, покрыт тестами; `run_eval.py --live` меряет реальный GLM-5,
`--demo` — проверка трубы. Харнесс сразу нашёл дефект дизайна (утечка слов инструкций
в область кода — исправлено переносом инструкций в system-промпт).

## Схема потока (целевая, после активации)

```
webhook large-PR ──► classify(list_pull_files) ──► fast-ack (ФТ-APRP-3)
       │                                              │
       ▼                                              ▼
   plan_chunks (ФТ-APRP-4/5) ──► create_job ──► fan-out chunk-события (ФТ-APRP-6)
                                                        │
                                   ┌────────────────────┴───────────┐
                                   ▼ (параллельно, deep-tier GLM-5)  │
                          review_chunk [ПУНКТ A] ──► record_finding  │ progress N/M
                                   │                                  │ (ФТ-APRP-9)
                                   ▼                                  │
                          claim_reduce (CAS M4) ──► collect ──► synthesize (ФТ-APRP-8)
                                                                      │
                                                                      ▼
                                                        publish единый review (СТ-25)
```

## Следующий шаг
Согласовать промпт/подход ревью чанка (пункт A) → реализовать адаптер → активировать
в воркере (пункт B) на стейджинге → калибровка (C) → включить в прод мержем в `main`.
