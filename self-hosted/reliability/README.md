# reliability — доменное ядро отказоустойчивости (Фаза 1)

Реализация проверяемых СТ из [`../SYSTEM-REQUIREMENTS.md`](../SYSTEM-REQUIREMENTS.md)
(трекинг — issue #1). Модули без внешних зависимостей (stdlib-only), покрыты
`unittest`.

## Что реализовано

| Модуль | СТ | Назначение |
|---|---|---|
| `security.py` | СТ-1 | HMAC-проверка webhook `X-Hub-Signature-256` |
| `state.py` | СТ-2, 10, 11, 12, 13, 16, 28 | dedup по `delivery_id`, машина состояний (CAS), идемпотентный эффект, учёт попыток, детект «застрявших» |
| `notifier.py` | СТ-27 | текст видимого комментария о провале в PR |
| `webhook.py` | СТ-8 | разбор payload → `Event` (PR-события, slash-команды) |
| `github_client.py` | СТ-27 | публикация комментария (инъект токен/транспорт, zero-dep) |
| `supervisor.py` | СТ-14..16, 27 | оркестрация: success/fail/dead-letter → **коммент в PR при провале** |
| `app.py` | СТ-1..5 | FastAPI-ingress (тонкая обвязка над модулями выше) |
| `analyze_adapter.py` | — | **интеграционный шов** (минтинг токена + запуск pr-agent) — стаб, следующий срез |

## Запуск тестов

```bash
cd self-hosted && python3 -m unittest discover -s reliability/tests -t . -v
```

Внешних зависимостей нет — работает на любом Python 3.11+.

## Статус и что дальше

Логика ingress/супервизора **написана и покрыта тестами**, но end-to-end ещё не
работает: нужен `analyze_adapter` (минтинг installation-токена + запуск pr-agent).
До этого `app.py` не разворачивается в проде.

- **Следующий срез:** реализовать `analyze_adapter` (App JWT → токен; запуск анализа) → первый живой прогон «не молчать».
- Durable queue + split ingress/worker (СТ-6..9, 14..18) — Фаза 2.
- Actioning stale + метрика/алерт dead-letter (добить СТ-13, СТ-27 б/в); reconciliation sweeper (СТ-29..32); LLM Gateway (СТ-19..24); метрики (СТ-33..35) — Фазы 3–4.
