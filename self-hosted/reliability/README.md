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
| `github_client.py` | СТ-25,27 | **идемпотентная публикация** (upsert по маркеру: правит бот-коммент, а не плодит дубли) |
| `supervisor.py` | СТ-14..16, 27 | оркестрация: success/fail/dead-letter → **коммент в PR при провале** |
| `ingress.py` | СТ-1,2,4 | приём webhook: подпись, dedup, устойчивость к битому payload |
| `app.py` | СТ-3,5 | FastAPI-обвязка над `ingress` |
| `token.py` | — | App JWT → installation token с кэшем (оркестрация тестируется) |
| `analyze_adapter.py` | — | запуск анализа pr-agent + токен; реальные обёртки — на деплое |
| `metrics.py` | СТ-27б | счётчики (`dead_letter_total`, `reconcile_escalated_total`) |
| `sweeper.py` | СТ-13, 29..32 | **reconciliation**: застрявшие→retry/dead-letter, PR без ревью→reconcile, эскалация |
| `queue.py` | СТ-6..9 | **durable queue**: at-least-once, visibility-timeout redelivery, DLQ, фенсинг, честность по партициям |
| `worker.py` | СТ-14..18 | **worker loop**: lease→process(per-task таймаут)→ack/nack; при DLQ — коммент в PR (СТ-27) |
| `sweeper_adapter.py` | — | реальные порты свипера: `list_open_prs`, `has_completed_review` (через store) |
| `sweeper_runner.py` | — | периодический раннер свипера (deploy entrypoint) |

## Запуск тестов

```bash
cd self-hosted && python3 -m unittest discover -s reliability/tests -t . -v
```

Внешних зависимостей нет — работает на любом Python 3.11+.

## Статус

Весь путь reliability написан и покрыт тестами (**107 unittest**): приём → dedup →
обогащение → атомарный захват → состояние → запуск анализа → при hard-сбое коммент
в PR → reconcile. Реальные интеграционные обёртки (`_real_invoke` pr-agent,
RS256-подпись, urllib, GitHub-порты) отмечены `pragma: no cover` и проверяются на
первом деплое.

## Как поднять (go live) — артефакты и шаги

Готовые артефакты (в `self-hosted/`, отдельно от прод-`docker-compose.yml`):
`Dockerfile.reliability` · `reliability-entrypoint.sh` · `docker-compose.reliability.yml`.

Три процесса на общем томе с SQLite (state + queue):
- **ingress** — `uvicorn reliability.app:app` (принимает webhook → durable queue);
- **worker** — `python -m reliability.worker` (очередь → pr-agent → ack/nack, коммент при DLQ);
- **sweeper** — `python -m reliability.sweeper_runner` (периодически дозапускает пропущенное/застрявшее).

Шаги:
1. `docker compose -f docker-compose.yml build` — собрать базу `pr-agent-github-app:local`.
2. Заполнить env (та же `dokploy.env` + `RELIABILITY_REPOS="owner/repo,..."`).
3. `docker compose -f docker-compose.reliability.yml up -d`.
4. Webhook GitHub App → `/webhook` (ingress); healthcheck → `/health`. Обновить `origin`/webhook на новый адрес репо.

⚠️ Это меняет прод-поведение — сначала стейджинг. `pragma: no cover` обёртки
(`_real_invoke`, RS256-подпись, `make_list_open_prs`, раннеры) проверяются именно
на этом смоуке (см. Deploy-чеклист ниже).

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

## Reconciliation sweeper — статус

Логика `sweep()` реализована и покрыта тестами (СТ-13, 29..32): застрявшие вне
терминала → свежий retry или dead-letter; открытые PR без подтверждённого ревью →
reconcile-enqueue с `force` (обходит `already_done`); эскалация после `max_cycles`
циклов.

Строгость критерия «ревью есть» задаёт инъектируемый порт `has_completed_review`
(`sweeper_adapter.make_has_completed_review(store, verify=None)`):
- без `verify` — store-only: `already_done` закрывает **пропущенные webhook'и,
  необработанные и застрявшие PR** (нет DONE-строки → reconcile);
- с `verify` — DONE-строка перепроверяется артефактом на GitHub. Опорный
  `make_github_review_verifier(client)` считает доказательством активность бота на
  PR (`client.has_bot_activity`). Если DONE в сторе, но артефакта нет — это
  **«проглоченный» сбой** → reconcile. `verify` инъектируется, чтобы точную
  эвристику артефакта (formal review vs. коммент, привязка к head_sha) донастроить
  на смоуке, не трогая логику свипера.

Уже собрано для деплоя (`sweeper_adapter` + `sweeper_runner`):
- периодический раннер (loop, интервал `RELIABILITY_SWEEP_INTERVAL`);
- порт `list_open_prs()` через `client.list_open_pulls` (GitHub API, пагинация);
- порт `has_completed_review(...)` — store + опциональный GitHub-verify (выше);
- `enqueue(event, force=…)` → `supervisor.process(..., force=force)`.

## Дальше по фазам

- **Фаза 2 закрыта (логика):** durable queue ✅ + worker/split ✅. Поток теперь
  `ingress (app.py) → queue → worker (process с таймаутом) → ack/nack`; ретрай/DLQ
  на очереди, эскалация (коммент в PR) — воркером при исчерпании выдач.
- Блок B (полировка) ✅: атомарный claim бизнес-ключа (СТ-16 — устранён остаточный
  double-analyze при гонке доставок; захват в `state.try_claim/release_claim`,
  проигравший — skip+ack), обогащение head_sha для issue_comment (`webhook.enrich_events`
  + `client.get_pull_head_sha`), детект проглоченного сбоя (`has_completed_review`
  с опциональным GitHub-verify). upsert-публикатор СТ-25 ✅.
- **B4 (осталось)** — консолидация слоёв ретрая: убрать пересечение
  sweeper-stale ↔ queue-redelivery (после split часть страховок sweeper дублирует
  visibility-timeout очереди). Это рефактор живой семантики очереди — вернее
  проверять на стейджинг-смоуке, чем менять вслепую.
- Наблюдаемость poison-guard: DLQ на `lease` (повторные краши без nack) сейчас
  эскалируется только через sweeper (окно ~stale_deadline); дать метрику/коммент в моменте.
- СТ-18 автоскейл воркеров по глубине/возрасту очереди — Фаза 4 (сейчас только `queue.depth()`).
- Ретеншн `queue.partition_service` (растёт по числу репозиториев — очистка/prune на масштабе).
- LLM Gateway (СТ-19..24), полная observability/алерты (СТ-33..35) — Фазы 3–4.

> Note: SQLite-очередь durable в пределах одного узла Dokploy. Несколько узлов
> одновременно — заменить реализацию на Redis/RabbitMQ за тем же интерфейсом
> (`enqueue`/`lease`/`ack`/`nack`).
