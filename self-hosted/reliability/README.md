# reliability — доменное ядро отказоустойчивости (Фаза 1)

Реализация проверяемых СТ из [`../SYSTEM-REQUIREMENTS.md`](../SYSTEM-REQUIREMENTS.md)
(трекинг — issue #1). Модули без внешних зависимостей (stdlib-only), покрыты
`unittest`.

## Что реализовано в этом срезе

| Модуль | СТ | Назначение |
|---|---|---|
| `security.py` | СТ-1 | HMAC-проверка webhook `X-Hub-Signature-256` |
| `state.py` | СТ-2, 10, 11, 12, 13, 16, 28 | dedup по `delivery_id`, машина состояний, идемпотентный эффект по бизнес-ключу, учёт попыток, детект «застрявших» событий |
| `notifier.py` | СТ-27 | **видимый комментарий о провале в PR** — ключевой пункт «не молчать» |

## Запуск тестов

```bash
cd self-hosted && python3 -m unittest discover -s reliability/tests -t . -v
```

Внешних зависимостей нет — работает на любом Python 3.11+.

## Следующие срезы (по SYSTEM-REQUIREMENTS.md)

- FastAPI-ingress поверх `security`+`state` (СТ-3,4,5) + реальный GitHub-клиент для `notifier`.
- Durable queue + split ingress/worker (СТ-6..9, 14..18) — Фаза 2.
- LLM Gateway (СТ-19..24), reconciliation sweeper (СТ-29..32), метрики/алерты (СТ-33..35) — Фазы 3–4.
