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
| — | Оркестрация (route/fan-out payloads/claim_reduce/collect) | `mapreduce.py` |
| — | Источник данных (изменённые файлы PR) | `github_client.py::list_pull_files` |

## Осталось — требует решения/активации

| # | Пункт | Почему не сделано автономно |
|---|---|---|
| A | **Адаптер ревью чанка** (реальный вызов модели + промпт code-review для набора файлов) | Качество — главный критерий (Q4); промпт задаёт качество и должен быть согласован. Нужен метод оценки качества (Q8). pr-agent ревьюит весь PR (не подходит для чанка) → нужен прямой вызов GLM-5 с промптом |
| B | **Активация в воркере** (dispatch event_type `chunk`/`reduce`, маршрутизация large-PR через map-reduce) | Мерж в `main` = автодеплой. Включать map-reduce в живой воркер — как «go-live»-переключение: осознанный шаг после проверки A на стейджинге, чтобы не сломать текущий путь |
| C | **Калибровка порога/размера чанка на GLM-5/GLM-4.7** (Q2 числа) | Требует замера латентности vs размер на реальной модели |
| ФТ-APRP-12 | Кэш ревью по blob-sha (опц.) | Followup (Q7) |

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
