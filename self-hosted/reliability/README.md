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
| `ingress.py` | СТ-1,2,4 | приём webhook: подпись, dedup, устойчивость к битому payload |
| `app.py` | СТ-3,5 | FastAPI-обвязка над `ingress` |
| `token.py` | — | App JWT → installation token с кэшем (оркестрация тестируется) |
| `analyze_adapter.py` | — | запуск анализа pr-agent + токен; реальные обёртки — на деплое |
| `metrics.py` | СТ-27б | счётчики (`dead_letter_total`; зерно под СТ-33) |

## Запуск тестов

```bash
cd self-hosted && python3 -m unittest discover -s reliability/tests -t . -v
```

Внешних зависимостей нет — работает на любом Python 3.11+.

## Статус

Весь путь reliability написан и покрыт тестами (**45 unittest**): приём → dedup →
состояние → запуск анализа → при hard-сбое коммент в PR. Реальные интеграционные
обёртки (`_real_invoke` pr-agent, RS256-подпись, urllib) отмечены `pragma: no cover`
и проверяются на первом деплое.

## Как поднять (go live) — шаг для деплоя

Ingress заменяет webhook-вход pr-agent и вызывает его анализ как библиотеку, чтобы
видеть исход. Нужно:

1. Образ содержит и pr-agent, и пакет `reliability` (тот же контейнер).
2. Entrypoint запускает `reliability.app:app` (uvicorn) вместо `pr_agent.servers.github_app:app`.
3. Env: `GITHUB_WEBHOOK_SECRET`, `GITHUB_APP_ID`, `GITHUB_PRIVATE_KEY_B64` (уже есть),
   `RELIABILITY_DB` (путь SQLite на volume), `RELIABILITY_MAX_ATTEMPTS`.
4. Webhook GitHub App → `/webhook`; healthcheck → `/health`.

⚠️ Живой прогон меняет поведение контейнера — применяйте на тесте до прод-переключения.
`docker-compose.yml` намеренно НЕ тронут; переключение entrypoint — отдельный
осознанный шаг (см. issue #1).

## Deploy-чеклист (риски go-live — проверить в стейджинге)

Не блокеры, но проверить на первом реальном прогоне (юнит-тесты их по построению
не видят):

- [ ] **Инвариант «`process` остаётся sync».** `_real_invoke` делает `asyncio.run`; безопасно, только пока супервизор гонится в threadpool без активного loop. Не делать `process`/`schedule` `async def`.
- [ ] **Насыщение threadpool.** Анализ pr-agent (до ~90 c) держит поток из дефолтного пула. До очереди (Фаза 2) всплеск webhook'ов может выесть пул — ограничить конкуррентность или ускорить перенос на очередь.
- [ ] **RS256 и формат ключа.** В образе должен быть `cryptography` (иначе PyJWT падает на RS256); `GITHUB_PRIVATE_KEY_B64` → PEM того формата, что отдаёт GitHub.
- [ ] **Кэш токена — на процесс.** При нескольких uvicorn-воркерах кэш не общий (больше вызовов GitHub, но корректно).
- [ ] **Смоук-тест.** Один реальный минтинг токена + один `_real_invoke` в стейджинге закрывают риски выше.

## Дальше по фазам

- Durable queue + split ingress/worker (СТ-6..9, 14..18) — Фаза 2.
- Actioning stale + reconciliation sweeper (СТ-13, 29..32) — закрывает «проглоченный» сбой.
- Добить СТ-16 (атомарный claim + upsert СТ-25), СТ-14 (таймаут), обогащение head_sha.
- LLM Gateway (СТ-19..24), метрики/алерты (СТ-33..35) — Фазы 3–4.
