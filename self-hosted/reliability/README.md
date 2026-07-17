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
| `sweeper_adapter.py` | — | реальные порты свипера: `list_open_prs`, `has_completed_review` (store + опц. GitHub-verify) |
| `sweeper_runner.py` | — | периодический раннер свипера (deploy entrypoint) |
| `gateway.py` | СТ-19..24 | **LLM Gateway**: circuit breaker + token-bucket rate limit + failover по пулу + таймаут попытки (аутейдж Z.AI → быстрый видимый отказ, не тишина) |
| `autoscale.py` | СТ-18 | политика числа воркеров по глубине/возрасту очереди (исполняет оркестратор) |
| `metrics.py` | СТ-27б,33..35 | счётчики + `render_prometheus` → `/metrics` (наблюдаемость К-5) |

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

## Как поднять (go live)

**Прод-`docker-compose.yml` теперь ЗАПУСКАЕТ reliability-стек** (ingress/worker/sweeper),
а не «голый» webhook pr-agent. Полная процедура, приёмка и откат — в
[`../GO-LIVE.md`](../GO-LIVE.md); смоук-проверка — [`../scripts/smoke.sh`](../scripts/smoke.sh).

Артефакты (в `self-hosted/`): `docker-compose.yml` (прод-стек) · `Dockerfile.reliability`
· `reliability-entrypoint.sh` · `docker-compose.legacy-pr-agent.yml` (откат на прежнее поведение).

Три процесса на общем томе с SQLite (state + queue):
- **ingress** — `uvicorn reliability.app:app` (webhook → durable queue; `/health`, `/metrics`);
- **worker** — `python -m reliability.worker` (очередь → pr-agent через LLM Gateway → ack/nack, коммент при DLQ);
- **sweeper** — `python -m reliability.sweeper_runner` (периодически дозапускает пропущенное/застрявшее).

Кратко: `docker compose up -d --build` (соберёт базу `pr-agent-base` → reliability-образ),
env как раньше + `RELIABILITY_REPOS`, webhook GitHub App → `:3000/webhook`. Детали — `GO-LIVE.md`.

⚠️ `pragma: no cover` обёртки (`_real_invoke`, RS256-подпись, GitHub-порты, раннеры)
проверяются именно на первом смоуке (см. Deploy-чеклист ниже) — до прод-переключения
webhook прогнать на тестовом PR. Откат: `docker-compose.legacy-pr-agent.yml`.

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
- **Блок C (Фазы 4–5) — логика готова ✅:**
  - LLM Gateway (СТ-19..24, `gateway.py`): circuit breaker + rate limit + failover +
    таймаут; вкручен в `worker.main` (один провайдер Z.AI, seam под добавление ключей).
    rate limit **процессный** → при N воркерах суммарный RPS ≈ N×rate: задавать
    `RELIABILITY_LLM_RPS ≈ (лимит Z.AI)/(макс. реплик)` или вынести лимитер в Redis.
    Rate-limit = backpressure (`state.Backpressure`): воркер откладывает событие
    (`queue.defer`, `RELIABILITY_BACKPRESSURE_DELAY`) БЕЗ счёта к DLQ и без ложного
    коммента; реальные сбои ретраятся с `RELIABILITY_BACKOFF`×attempts. Класс сбоя
    (GatewayUnavailable / Z.AI-ошибка / таймаут) доходит до DLQ-коммента и метрики.
  - Observability (СТ-33..35): `/metrics` (Prometheus) отдаёт счётчики + `queue_depth`/
    `dead_letters`. Алерт-роутинг (healthchecks.io) — отдельный issue (уже отложен).
  - Автоскейл (СТ-18, `autoscale.py`): политика `desired_workers`; ИСПОЛНЕНИЕ —
    оркестратор (compose scale / k8s HPA), опрашивает `/metrics` и применяет число.
- **Осталось (deploy-shaped / B4):**
  - Ретеншн `queue.partition_service` (растёт по числу репозиториев — prune на масштабе).
  - Консолидация sweeper-stale ↔ queue-redelivery (B4) — тюнить на смоуке.
  - Потолок GitHub API rate-limit: ETag-кэш, шардирование App (`SCALE-PLAN.md §3`).

> Note: SQLite-очередь durable в пределах одного узла Dokploy. Несколько узлов
> одновременно — заменить реализацию на Redis/RabbitMQ за тем же интерфейсом
> (`enqueue`/`lease`/`ack`/`nack`).
