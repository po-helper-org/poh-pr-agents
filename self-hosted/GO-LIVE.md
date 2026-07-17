# GO-LIVE — переключение прода на reliability-стек (issue #1)

С этого коммита прод-`docker-compose.yml` запускает отказоустойчивый слой
`reliability/` (**ingress → durable queue → worker → sweeper**) вместо «голого»
webhook-сервера pr-agent. Цель — **«не молчать»**: любой сбой/зависание Z.AI даёт
видимый коммент в PR, а не тишину.

> ⚠️ Это меняет прод-поведение. Сначала прогнать смоук (лучше на тестовом
> репо/канале), потом переключать боевой webhook. Откат — одной командой (см. ниже).

---

## 0. Что меняется

| | Было (`docker-compose.legacy-pr-agent.yml`) | Стало (`docker-compose.yml`) |
|---|---|---|
| Процессы | один `pr-agent` (gunicorn webhook) | `ingress` + `worker` + `sweeper` |
| Webhook | `:3000/api/v1/github_webhooks` | `:3000/webhook` **и** `:3000/api/v1/github_webhooks` (легаси-путь тоже принимается) |
| Healthcheck | TCP :3000 | `:3000/health` |
| Метрики | нет | `:3000/metrics` (Prometheus) |
| При сбое LLM | тишина (лог, без коммента) | ретрай→DLQ→**видимый коммент в PR** + метрика |
| Пропущенный webhook | потерян | дозапуск свипером |

Webhook-URL App'а менять **не требуется**: ingress принимает и легаси-путь
`/api/v1/github_webhooks`, и `/webhook`. Репозиторий переехал → `po-helper-org/poh-pr-agents`.

---

## 1. Env (Dokploy → Environment)

Обязательные (как раньше): `OPENAI_KEY`, `GITHUB_APP_ID`, `GITHUB_WEBHOOK_SECRET`,
`GITHUB_PRIVATE_KEY_B64` (one-line base64 PEM — см. SETUP.md).

Новое обязательное для свипера:
```
RELIABILITY_REPOS="owner/repo,owner/repo2"     # какие открытые PR сверять; пусто → sweeper бездействует
```

Опциональные (есть дефолты) — LLM Gateway и поведение:
```
RELIABILITY_LLM_RPS=3           # запросов/сек к Z.AI. ПРОЦЕССНЫЙ лимит: при N репликах
                                #   worker суммарно ≈ N×RPS → задавать (лимит Z.AI)/N.
RELIABILITY_LLM_BURST=6         # ёмкость всплеска токен-бакета
RELIABILITY_CB_THRESHOLD=5      # сбоев подряд → circuit размыкается (быстрый отказ)
RELIABILITY_CB_RESET=30         # сек до пробного вызова после размыкания
RELIABILITY_MAX_ATTEMPTS=5      # выдач без ack → dead-letter + коммент
RELIABILITY_BACKOFF=10          # сек ×attempts между ретраями (не спиним)
RELIABILITY_SWEEP_INTERVAL=300  # период сверки свипером, сек
RELIABILITY_STALE_DEADLINE=1800 # застряло дольше → retry/dead-letter
RELIABILITY_VERIFY_GITHUB=      # "1" — детект «проглоченного» сбоя (эвристику тюнить на смоуке)
```

---

## 2. Деплой

```bash
cd self-hosted
docker compose up -d --build          # соберёт pr-agent-base → reliability-образ, поднимет 3 сервиса
docker compose ps                     # ingress/worker/sweeper — Up; pr-agent-base — Exited(0) (это норма: он одноразовый)
docker compose logs -f worker sweeper # убедиться, что стартовали без трейсбеков
```

Если сборка reliability-образа падает с `pull access denied for pr-agent-github-app`
(compose не собрал базу раньше зависимого `FROM`) — собрать базу явно, затем поднять:
```bash
docker compose build pr-agent-base && docker compose up -d --build
```

`pr-agent-base` — одноразовый сервис только ради сборки базового образа
`pr-agent-github-app:local`; после сборки он завершается (`/bin/true`, `restart: no`).
Состояние `Exited (0)` — ожидаемо, не ошибка.

---

## 3. Смоук (автопроверки + ручные)

```bash
BASE_URL=http://127.0.0.1:3000 ./scripts/smoke.sh
```
Скрипт проверяет `/health` (200), `/metrics` (есть `reliability_*`), неподписанный
`/webhook` → 401 (HMAC СТ-1). Затем — **ручные** шаги (закрывают `pragma: no cover`
обёртки: RS256-подпись, `_real_invoke`, GitHub-порты):

1. **Реальное ревью.** Открыть тестовый PR → в течение ~1–2 мин появляется ревью.
   `/metrics`: `reliability_processed_ok` вырос. ← закрывает минтинг токена + `_real_invoke`.
2. **Видимый сбой (К-1).** Временно сломать LLM воркеру (неверный `OPENAI_KEY` или
   недоступный `OPENAI_API_BASE`) → повторить PR → после `RELIABILITY_MAX_ATTEMPTS`
   в PR **появляется коммент о провале** (не тишина); `/metrics`:
   `reliability_dead_letter_total` вырос, `gateway_provider_failure`/`gateway_unavailable`
   ненулевые. Вернуть корректный ключ.
3. **Chaos: смерть воркера.** `docker compose restart worker` в момент анализа →
   задача передоставлена и завершена, **дублей ревью/коммента нет** (СТ-25/idempotency).
4. **Chaos: пропуск webhook.** Остановить ingress на минуту, вернуть → sweeper
   дозапускает пропущенный PR (в пределах `RELIABILITY_SWEEP_INTERVAL`).

Приёмка go-live = все 4 прошли (соответствует `SYSTEM-REQUIREMENTS.md §13/§14`).

---

## 4. Боевой webhook

**Менять webhook-URL App'а НЕ обязательно.** Ingress принимает и новый `/webhook`,
и легаси-путь `/api/v1/github_webhooks` — существующий App продолжает слать на
легаси-путь, и он работает на новом стеке. Go-live бесшовен и легко откатывается.

После смоука:
1. GitHub App → Advanced → Recent Deliveries → Redeliver → ждём `200`.
2. (Опционально) можно переключить Webhook URL на `.../webhook` — но это не требуется.
3. `origin`/webhook уже на новом репо `po-helper-org/poh-pr-agents`.

---

## 5. Откат (rollback)

Если смоук/прод дал сбой — вернуть прежнее поведение одной командой:
```bash
docker compose down
docker compose -f docker-compose.legacy-pr-agent.yml up -d --build
```
И вернуть Webhook URL GitHub App на `.../api/v1/github_webhooks`. Данные reliability
(volume `reliability-data`) при этом сохраняются — можно вернуться на новый стек позже.

---

## 6. Известные ограничения (проверить/учесть на масштабе)

- **Rate limit и circuit breaker — процессные** (in-memory на реплику). При N воркерах
  суммарный RPS ≈ N×`RELIABILITY_LLM_RPS`; размыкание цепи независимо на каждой реплике.
  Для одного узла Dokploy (где и SQLite-очередь одноузловая) приемлемо; на несколько
  узлов — общий лимитер/очередь в Redis за тем же интерфейсом.
- **Один ключ Z.AI** — кросс-провайдерного резерва нет; gateway готов принять доп.
  провайдеров (`Provider(...)` в `worker.main`), когда появится второй ключ/эндпоинт.
- **Алерты** — пока только видимый коммент в PR + `/metrics`. Интеграция healthchecks.io
  — отдельный issue (осознанно отложено).
- **B4** — консолидация sweeper-stale ↔ queue-redelivery; ретеншн `partition_service`;
  ETag-кэш/шардирование App против потолка GitHub API — тюнятся на реальной нагрузке.
