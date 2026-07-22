# DEPLOY — от нуля до продакшена под нагрузкой

Сквозная инструкция: развернуть self-hosted **отказоустойчивый PR-Agent** (авто
CodeReview на GLM-5 через Z.AI) на **Dokploy** — так, чтобы любой сбой становился
видимым комментарием в PR, а не тишиной.

Идёшь сверху вниз, ничего не пропуская. Оценка времени — 15–25 минут.

- Как всё устроено и как чинить сбои: [`ARCHITECTURE.md`](ARCHITECTURE.md)
- Тонкие грабли деплоя (traefik, env, PEM): [`SETUP.md`](SETUP.md)
- Переключение с прежнего «голого» pr-agent и откат: [`GO-LIVE.md`](GO-LIVE.md)

---

## 0. Что ты разворачиваешь

Один Dokploy-сервис типа **Compose** поднимает **три процесса** на общем томе:

| Процесс | Роль |
|---|---|
| **ingress** | принимает webhook GitHub, кладёт в durable-очередь, отвечает мгновенно `200` |
| **worker** | очередь → анализ pr-agent (через LLM Gateway) → при сбое **коммент о провале в PR** |
| **sweeper** | раз в 5 мин дозапускает пропущенное/застрявшее |

Контракт — SLO: **нет тихих падений · гарантированное завершение · идемпотентность ·
наблюдаемость** (диаграмма — в `ARCHITECTURE.md`).

---

## 1. Предпосылки

- **Dokploy** и хостнейм, который на него резолвится (например `pr-agent.sub.traefik.me`).
- **Z.AI GLM-5 API key** (`OPENAI_KEY`).
- Права **админа GitHub-организации** (создать/настроить GitHub App).
- Билдеру Dokploy нужно ~**4 GB RAM / 2 CPU** (сборка образа pr-agent).
- Локально: `curl`, `python3`, `base64`, `openssl` (для генерации секретов).

---

## 2. GitHub App — создать или взять существующий

PR-Agent работает как **GitHub App** (webhook), а не GitHub Actions.

### 2A. Приложения ещё нет — создать за 2 минуты

1. Открой в браузере [`register-app.html`](register-app.html) (просто файл из репозитория).
2. Введи `owner/repo` (напр. `po-helper-org/poh-pr-agents`) и **webhook base URL** —
   это адрес будущего сервиса, напр. `http://pr-agent.sub.traefik.me` (см. заметку про
   traefik.me в `SETUP.md`). Нажми **Create GitHub App**.
3. GitHub редиректнет на `<base>/?code=…` (страница может не открыться — это нормально).
   Скопируй `code` из адресной строки и выполни локально:
   ```bash
   bash finish-registration.sh <code>
   ```
   Скрипт обменяет код на учётные данные и запишет файл **`dokploy.env`** уже с
   `GITHUB_APP_ID`, `GITHUB_WEBHOOK_SECRET`, `GITHUB_PRIVATE_KEY_B64` (и плейсхолдером
   `OPENAI_KEY`). Приватный ключ никуда не уходит — только в этот локальный файл.
4. Скрипт напечатает **install-ссылку** (`https://github.com/apps/<slug>/installations/new`) —
   открой её и **установи App на репозиторий**.
5. Готово — переходи к **§3** (значения уже в `dokploy.env`).

> App создаётся с правами `pull_requests:write, issues:write, contents:read,
> metadata:read` и событиями `pull_request, issue_comment, push`, webhook-путь —
> `/api/v1/github_webhooks`. Менять ничего не нужно: наш ingress принимает и этот
> легаси-путь, и `/webhook`.

### 2B. App уже есть — где найти и как достать секреты

Открой: **github.com → организация → Settings → Developer settings → GitHub Apps →
твой App → General** (личный App: `github.com/settings/apps`).

- **`GITHUB_APP_ID`** — число «App ID» вверху страницы. Просто скопируй.
- **`GITHUB_WEBHOOK_SECRET`** — GitHub **не показывает** старый секрет повторно:
  - знаешь его (из старого `dokploy.env`/менеджера паролей) → используй;
  - нет → сгенерируй новый `openssl rand -hex 32`, впиши в App → раздел **Webhook →
    Secret → Save changes**, и **то же значение** положи в env (должны совпадать,
    иначе ingress ответит `401`).
- **`GITHUB_PRIVATE_KEY_B64`** — App → раздел **Private keys → Generate a private key**
  → скачается `.pem`. Закодируй в **одну строку**:
  ```bash
  base64 < имя-app.*.private-key.pem | tr -d '\n' > key.b64.txt
  ```
  `tr -d '\n'` обязателен — значение должно быть без переносов. Содержимое
  `key.b64.txt` целиком → в `GITHUB_PRIVATE_KEY_B64`.
  Проверка: `echo "<строка>" | base64 -d | head -1` → `-----BEGIN ... PRIVATE KEY-----`.

---

## 3. Переменные окружения (итог)

Четыре обязательные + одна для свипера:

```
OPENAI_KEY=<ключ Z.AI GLM-5>
GITHUB_APP_ID=<число со страницы General>
GITHUB_WEBHOOK_SECRET=<из §2>
GITHUB_PRIVATE_KEY_B64=<одна строка из §2>
RELIABILITY_REPOS=po-helper-org/*,ai-oxudevelopment/*
```

`RELIABILITY_REPOS` — область для **свипера** (бэкстопа на пропущенные webhook'и):
- точные `owner/repo,owner/repo2` — сверять ровно эти;
- **маска `owner/*`** — все репо этой орг/аккаунта, включая будущие (несколько орг
  через запятую: `po-helper-org/*,ai-oxudevelopment/*`); можно мешать с точными;
  `*` — все репо всех установок;
- **пусто → org-wide**: то же, что `*` — свипер сам обходит **все репозитории всех
  установок App** (несколько орг/аккаунтов сразу, включая новые) — список не ведёшь.

> Маска раскрывается через установки App (по `account.login`) на каждом проходе
> свипера, поэтому новые репо орг подхватываются сами. Если App не установлен на
> указанный owner — маска молча раскрывается в пусто (свипер не падает; живые ревью
> и так идут по webhook). Голый owner без `/repo` (напр. `po-helper-org`) тоже
> трактуется как маска `owner/*`.

⚠️ Живые ревью на PR и так идут по **webhook** для любого репо, где установлен App
(§«Все репозитории организации» ниже) — свипер лишь страховка. Остальные настройки
(LLM Gateway, интервалы) имеют дефолты (полный список — в конце `GO-LIVE.md`).

### Все репозитории организаций/аккаунтов

Чтобы обрабатывать несколько орг/аккаунтов (напр. `po-helper-org`,
`ai-oxudevelopment` и репо `kibarik/mts-po-workspace`):

1. **Установи один и тот же GitHub App на каждый** аккаунт/орг:
   - для орга — Install → **All repositories** (тогда и будущие репо покрыты);
   - для аккаунта `kibarik` — Install → **Only select repositories** → `mts-po-workspace`.
   Один App = несколько установок (installations); webhook'и всех репо приходят на
   один и тот же ingress.
2. **Задай охват свипера** через `RELIABILITY_REPOS`:
   - хочешь **ровно эти орги** (напр. `po-helper-org` и `ai-oxudevelopment`, а App стоит
     ещё где-то) → маски: `RELIABILITY_REPOS=po-helper-org/*,ai-oxudevelopment/*`;
   - хочешь **вообще все установки** → оставь **пустым** (то же, что `*`).
   В обоих случаях перечислять репо руками не нужно — новые подхватываются сами.

Живые PR-события → мгновенное ревью по webhook (для любого установленного репо);
пропущенные — добирает свипер по расписанию.

---

## 4. Развернуть на Dokploy

> ⚠️ Тип сервиса — **Compose**, НЕ «Application». У нас три контейнера на общем томе;
> «Application» (с выбором **Build Type**: Dockerfile/Nixpacks/…) собирает лишь один
> контейнер и весь смысл отказоустойчивости теряет. Если видишь экран **Build Type** —
> ты создал не тот тип сервиса.

1. **Create Service → Compose** (Docker Compose).
2. **Provider:**
   - GitHub Account → твоя организация; Repository → `poh-pr-agents`.
   - **Branch** → `claude/github-app-unresponsive-02gk9m` (пока PR не смержен в `main`;
     после мержа — `main`).
   - Watch Paths (опц.) → `self-hosted/**` (редеплой только на изменения в папке).
3. **Compose Path** (на вкладке Compose/General) → **`self-hosted/docker-compose.yml`**.
   Это ключевое поле: без него Dokploy ищет compose в корне и не найдёт наш.
4. **Environment** → вставь блок из **§3**.
5. **Domains** → Host = твой хостнейм, **Container Port `3000`**. Если traefik.me —
   URL `http://`, HTTPS/redirect выключены (грабля #8 в `SETUP.md`).
6. **Deploy**.

Если сборка падает с `pull access denied for pr-agent-github-app` (Dokploy собрал
зависимый образ раньше базового) — в терминале сервиса:
```bash
docker compose build pr-agent-base && docker compose up -d --build
```

---

## 5. Проверить, что поднялось

```bash
docker compose ps
```
Ожидаемо: **ingress / worker / sweeper — `Up`**; **`pr-agent-base` — `Exited (0)`**
(норма: он одноразовый — только собирает базовый образ).

```bash
docker compose logs -f worker sweeper ingress   # без трейсбеков на старте
```

---

## 6. Смоук (быстрые проверки)

С хоста, где доступен ingress:
```bash
cd self-hosted && BASE_URL=http://127.0.0.1:3000 ./scripts/smoke.sh
```
Проверяет `/health` → 200, `/metrics` → есть `reliability_*`, неподписанный
`/webhook` → 401. Вручную: `curl -s http://<host>:3000/metrics | grep reliability_`.

**Webhook менять не нужно** — ingress принимает и `/api/v1/github_webhooks`, и `/webhook`.
Проверь доставку: **GitHub App → Advanced → Recent Deliveries → Redeliver → `200`**.

---

## 7. Боевая проверка + «не молчим»

1. **Ревью работает.** Открой тестовый PR в настроенном репо → в течение ~1–2 мин
   появляется ревью/описание. `curl /metrics` → `reliability_processed_ok` вырос.
2. **Сбой виден (главное).** Временно поставь воркеру неверный `OPENAI_KEY` (или
   недоступный `OPENAI_API_BASE`) → повтори PR → после нескольких попыток в PR
   **появляется коммент о провале** (не тишина); `/metrics`:
   `reliability_dead_letter_total` вырос. Верни правильный ключ, редеплой.
3. *(опц. chaos)* `docker compose restart worker` в момент анализа → задача
   передоставится и завершится, **дублей ревью нет**.

Прошли 1–2 (и, по желанию, 3) — go-live состоялся.

---

## 8. Под нагрузкой

Что значит «работает под нагрузкой»: события не копятся, p95 «событие → ревью»
держится < 10 мин, сбои Z.AI не роняют систему, всё видно в метриках.

**Смотри `/metrics`** (Prometheus; можно подключить Grafana/agent):

| Метрика | О чём говорит | Реакция |
|---|---|---|
| `reliability_queue_depth` | глубина бэклога | растёт устойчиво → добавь воркеров / подними RPS |
| `reliability_processed_ok` | успешные анализы | должна расти под нагрузкой |
| `reliability_dead_letter_total` | ушло в провал (есть коммент в PR) | всплеск → проблема с Z.AI/ключом |
| `reliability_gateway_circuit_open` | Z.AI лежит, цепь разомкнута | ждать/чинить Z.AI (сам закроется) |
| `reliability_gateway_rate_limited` / `reliability_backpressure_deferred` | упёрлись в свой лимит | подними `RELIABILITY_LLM_RPS`/`BURST` |

**Масштабирование воркеров.** Увеличь число реплик воркера (Dokploy → сервис →
масштаб, или `docker compose up -d --scale worker=N`). Политику «сколько воркеров»
по глубине/возрасту очереди считает `reliability/autoscale.py` (`desired_workers`) —
исполняет оркестратор.

**⚠️ Лимит Z.AI при нескольких воркерах.** Rate limit **процессный**: суммарный RPS ≈
N × `RELIABILITY_LLM_RPS`. Задавай:
```
RELIABILITY_LLM_RPS ≈ (лимит запросов Z.AI) / (макс. число реплик воркера)
```
Иначе N воркеров вместе пробьют лимит ключа. Аналогично circuit breaker — на реплику.

**Аутейдж Z.AI.** Circuit breaker гасит штормовые ретраи: после нескольких сбоев
подряд отказывает мгновенно (не виснет по 90с), а не выполненные PR подхватит
sweeper, когда Z.AI вернётся. Пользователь при этом видит коммент о недоступности.

**Потолки и следующий уровень** (Redis-очередь для мультиузла, ETag-кэш и
шардирование против лимитов GitHub API, пул провайдеров/ключей) — в
[`SCALE-PLAN.md`](SCALE-PLAN.md). Один узел Dokploy с SQLite-очередью — исходная
рабочая конфигурация; на неё этот гайд и рассчитан.

---

## 9. Если что-то не так

- **Диагностика по симптомам** (нет ревью / растёт очередь / залип circuit / дубли /
  застряло) — таблица + готовые SQLite-запросы в [`ARCHITECTURE.md §5`](ARCHITECTURE.md).
- **Откат** на прежний «голый» pr-agent:
  ```bash
  docker compose down
  docker compose -f docker-compose.legacy-pr-agent.yml up -d --build
  ```
  Данные (`reliability-data`) сохраняются — вернуться на новый стек можно позже.

---

## Куда смотреть дальше

| Документ | О чём |
|---|---|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | диаграммы конвейера + плейбук диагностики |
| [`SETUP.md`](SETUP.md) | справочник граблей (traefik, env, PEM, регион) |
| [`GO-LIVE.md`](GO-LIVE.md) | переключение прод-entrypoint, полный список env, откат |
| [`SYSTEM-REQUIREMENTS.md`](SYSTEM-REQUIREMENTS.md) · [`SCALE-PLAN.md`](SCALE-PLAN.md) | контракт К-1..К-5, СТ, путь к 100k/сутки |
