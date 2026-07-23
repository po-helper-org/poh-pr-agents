# FNR-1: Ревизия reliability/ — анализ заменяемости кастома на готовые инструменты

## Статус
**Открыт** — 2025-07-23

## Категория
Архитектурный анализ / рефакторинг

## Проблема

Self-hosted слой `reliability/` (~2200 строк Python, stdlib-only) реализует:
- ingress → durable queue → worker → LLM Gateway → sweeper
- Метрики, автоскейл, супервизор
- Состояние в SQLite

**Гипотеза:** значительная часть логики может быть заменена готовыми инструментами (брокер очереди, litellm-proxy, prometheus_client, KEDA) без потери доменных гарантий.

Требуется провести ревизию по существу: классифицировать компоненты на **заменяемые** (инфраструктурная логика) и **доменные** (бизнес-логика, связанная с notifier/sweeper/бизнес-ключами).

---

## Контекст

### Текущая архитектура

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         reliability/ слой                               │
├─────────────────────────────────────────────────────────────────────────┤
│  ingress.py (64) → queue.py (207) → worker.py (174)                     │
│                       ↓                                                 │
│                   state.py (294)                                        │
│                       ↓                                                 │
│              gateway.py (199) → analyze_adapter.py                      │
│                       ↓                                                 │
│                notifier.py (41) ──→ github_client.py (146)              │
│                                                                          │
│  Периодический: sweeper.py (121) ← sweeper_adapter.py (150)             │
│  Автоскейл: autoscale.py (28)                                          │
│  Метрики: metrics.py (60)                                              │
│  Безопасность: security.py (21)                                        │
└─────────────────────────────────────────────────────────────────────────┘
```

### Реализуемые Системные Требования (СТ)

Модули реализуют следующие СТ из `SYSTEM-REQUIREMENTS.md`:

| Модуль | СТ | Описание |
|--------|-----|----------|
| `security.py` | СТ-1 | HMAC-проверка webhook `X-Hub-Signature-256` |
| `state.py` | СТ-2,10,11,12,13,16,28 | Dedup, машина состояний (CAS), идемпотентность, учёт попыток, детект застрявших |
| `notifier.py` | СТ-27 | Видимый комментарий о провале в PR |
| `webhook.py` | СТ-8 | Разбор payload → `Event` (PR-события, slash-команды) |
| `github_client.py` | СТ-25,27 | Идемпотентная публикация (upsert) |
| `supervisor.py` | СТ-14..16,27 | Оркестрация: success/fail/dead-letter → коммент в PR |
| `ingress.py` | СТ-1,2,4 | Приём webhook: подпись, dedup, устойчивость к битому payload |
| `app.py` | СТ-3,5 | FastAPI-обвязка |
| `sweeper.py` | СТ-13,29..32 | Reconciliation: застрявшие→retry/dead-letter, PR без ревью→reconcile |
| `queue.py` | СТ-6..9 | Durable queue: at-least-once, visibility-timeout redelivery, DLQ, фенсинг |
| `worker.py` | СТ-14..18 | Worker loop: lease→process→ack/nack; при DLQ — коммент в PR |
| `gateway.py` | СТ-19..24 | LLM Gateway: circuit breaker + token-bucket rate limit + failover |
| `autoscale.py` | СТ-18 | Политика числа воркеров по глубине/возрасту очереди |
| `metrics.py` | СТ-27б,33..35 | Счётчики + `/metrics` (Prometheus) |

---

## Анализ компонентов

### 1. ЗАМЕНЯЕМЫЕ (готовые инструменты)

#### 1.1 `queue.py` (207 строк) → **RabbitMQ / Redis Streams / SQS**

**Что реализует:**
- Durable queue в SQLite
- At-least-once доставку
- Visibility-timeout с redelivery
- Dead-letter queue (DLQ)
- Фенсинг через lease token
- Partition service (честность между репозиториями)

**Код-доказательство:**
```python
# queue.py:36-43
class DurableQueue:
    def __init__(self, path: str = ":memory:", ...):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        ...
```

**Замена на:**
- **RabbitMQ**: DLQ natively, TTL/dead-letter exchange, acknowledgements
- **Redis Streams**: XREADGROUP, XACK, XPENDING, consumer groups
- **AWS SQS**: Visibility timeout, DLQ natively, at-least-once

**Сохранение гарантий:**
- Partition fairness → RabbitMQ consistent hashing или SQS MessageGroupId
- Фенсинг → messageId dedup на воркере

---

#### 1.2 `gateway.py` (199 строк) → **litellm-proxy / API Gateway**

**Что реализует:**
- Circuit breaker (СТ-22)
- Token-bucket rate limit (СТ-20)
- Failover по пулу провайдеров (СТ-19/21)
- Таймаут на попытку (СТ-14)

**Код-доказательство:**
```python
# gateway.py:64-102
class CircuitBreaker:
    def __init__(self, failure_threshold: int = 5, reset_timeout: float = 30.0, ...):
        self._threshold = failure_threshold
        self._failures = 0
        ...

class TokenBucket:
    def __init__(self, rate: float, capacity: float, ...):
        ...
```

**Замена на:**
- **litellm-proxy**: встроенный circuit breaker, rate limiting, failover
- **Kong** / **Azure API Management**: rate limiting, circuit breaking natively

**Сохранение гарантий:**
- Backpressure при открытых цепях → конфигурация retry policy

---

#### 1.3 `metrics.py` (60 строк) → **prometheus_client**

**Что реализует:**
- Thread-safe счётчики
- Рендер в Prometheus text exposition
- `/metrics` endpoint

**Код-доказательство:**
```python
# metrics.py:13-45
_counters: dict[str, int] = {}
_lock = threading.Lock()

def render_prometheus(gauges: Optional[dict] = None) -> str:
    ...
```

**Замена на:**
- **prometheus_client**: Counter, Gauge, Histogram natively
- **StatsD**:dogstatsd

**Сохранение гарантий:**
- Полностью — те же метрики, бо́льшая точность (histogram/percentile)

---

#### 1.4 `autoscale.py` (28 строк) → **KEDA / HPA**

**Что реализует:**
- Политика числа воркеров по `depth` и `oldest_age_s`
- Формула: `base + age_extra`, зажатая в `[min, max]`

**Код-доказательство:**
```python
# autoscale.py:12-28
def desired_workers(depth: int, oldest_age_s: float, *, per_worker: int = 20,
                    min_workers: int = 1, max_workers: int = 20,
                    age_pressure_s: float = 300.0) -> int:
    ...
```

**Замена на:**
- **KEDA** с RabbitMQ/Redis scaler: autoscaling по `depth` очереди natively
- **Kubernetes HPA** с custom metric от Prometheus

**Сохранение гарантий:**
- Age pressure → KEDA triggers based on lag threshold

---

#### 1.5 `security.py` (21 строка) → **pywebhooks / hmac из stdlib**

**Что реализует:**
- HMAC-проверка `X-Hub-Signature-256`
- Constant-time comparison

**Код-доказательство:**
```python
# security.py:12-21
def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)
```

**Замена на:**
- **webhooks** (PyPI): готовая библиотека для webhook verification
- Уже использует stdlib `hmac` — можно оставить как есть

---

### 2. ДОМЕННЫЕ (сохранить)

#### 2.1 `state.py` (294 строки) — **доменное состояние**

**Незаменяемо:** машина состояний с business-ключами

**Код-доказательство:**
```python
# state.py:15-35
class State(str, enum.Enum):
    RECEIVED = "received"
    QUEUED = "queued"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"

_ALLOWED: dict[State, frozenset[State]] = {
    State.RECEIVED: frozenset({State.QUEUED, State.FAILED}),
    ...
}
```

**Причины сохранения:**
- CAS-переходы (Compare-And-Swap) — защита от гонок
- `try_claim` / `release_claim` — атомарный захват бизнес-ключа
- `already_done` — идемпотентность по `(repo, number, head_sha, command)`
- Reconcile-циклы (`bump_reconcile`, `clear_reconcile`)

---

#### 2.2 `supervisor.py` (80 строк) — **оркестрация обработки**

**Незаменяемо:** логика одного прохода обработки

**Код-доказательство:**
```python
# supervisor.py:45-80
def process(event: Event, analyze: Analyze, store: StateStore, *, force: bool = False) -> Result:
    if not force and store.already_done(event.business_key):
        ...
    if not store.try_claim(event.business_key, event.delivery_id):
        return Result(..., skipped=True)
    ...
```

**Причины сохранения:**
- Драйв до DONE (безопасность от передоставки)
- Атомарный `try_claim` → пропустить двойную обработку
- Разделение Backpressure vs Failed

---

#### 2.3 `notifier.py` (41 строка) — **видимое оповещение**

**Незаменяемо:** форматирование и публикация провала в PR

**Код-доказательство:**
```python
# notifier.py:16-27
def build_failure_comment(command: str, error_class: str, attempts: int, escalated: bool) -> str:
    head = f"⚠️ Автоматический `{command}` не выполнен."
    detail = f"Причина: `{error_class}`. Попыток: {attempts}."
    ...
```

**Причины сохранения:**
- Доменный формат комментария (СТ-27 «не молчать»)
- Интеграция с `GitHubClient.upsert_comment`

---

#### 2.4 `webhook.py` (83 строки) — **парсинг GitHub webhook**

**Незаменяемо:** специфика GitHub-событий

**Код-доказательство:**
```python
# webhook.py:49-82
def parse_events(event_type: str, delivery_id: str, payload: dict, ...) -> list[Event]:
    if event_type == "pull_request":
        ...
        return [Event(delivery_id=f"{delivery_id}:{cmd}", ...) for cmd in pr_commands]
    ...
```

**Причины сохранения:**
- Mapping GitHub payload → `Event`
- Обогащение `head_sha` для issue_comment (запрос к GitHub API)
- Генерация нескольких команд на одно событие

---

#### 2.5 `github_client.py` (146 строк) — **GitHub API + upsert**

**Условно заменяемо:** транспорт можно заменить на `PyGithub`/`gidgethub`, но **upsert-логика** доменная

**Код-доказательство:**
```python
# github_client.py:125-146
def upsert_comment(self, repo: str, number: int, marker: str, body: str) -> None:
    tagged = f"{body}\n\n{marker}"
    matches = self._matching_comments(repo, number, marker)
    if not matches:
        self._transport("POST", ...)
        return
    self._transport("PATCH", ...)  # редактируем существующий
    for extra in matches[1:]:      # self-heal: удаляем дубли
        self._transport("DELETE", ...)
```

**Причины сохранения:**
- Upsert по HTML-маркеру (`<!-- reliability:... -->`) — СТ-25
- Пагинация (иначе на PR с >30 комментами маркер теряется)
- Self-heal: удаление дублей

**Замена частичная:** транспорт (`_urllib_transport`) → `PyGithub`/`gidgethub`, но upsert-логика сохранить.

---

#### 2.6 `sweeper.py` (121 строка) — **reconciliation логика**

**Незаменяемо:** доменная логика свипера

**Код-доказательство:**
```python
# sweeper.py:54-121
def sweep(store: StateStore, *, list_open_prs, has_completed_review, enqueue, ...):
    # 1) Застрявшие → retry/dead-letter
    for row in store.stale(stale_deadline):
        ...

    # 2) Открытые PR без ревью → reconcile-enqueue (force)
    for pr in list_open_prs():
        if has_completed_review(...):
            store.clear_reconcile(bkey)
            continue
        ...

    # 3) Эскалация после max_cycles
    if cycles > max_cycles:
        client.upsert_comment(...)
```

**Причины сохранения:**
- Детект застрявших (СТ-13)
- Reconcile-циклы с эскалацией (СТ-29..32)
- Инъектируемые порты `has_completed_review`, `list_open_prs`

---

#### 2.7 `sweeper_adapter.py` (150 строк) — **порты свипера**

**Незаменяемо:** связка свипера с GitHub/Store

**Код-доказательство:**
```python
# sweeper_adapter.py (реализация портов)
def make_has_completed_review(store, verify=None):
    if verify is None:
        return lambda repo, num, sha, cmd: store.already_done(bkey)
    ...
```

**Причины сохранения:**
- Store-only verify (go-live default)
- GitHub-verify (детект «проглоченного» сбоя)
- `list_open_prs` через GitHub API

---

#### 2.8 `worker.py` (174 строки) — **worker loop**

**Условно заменяемо:** цикл можно заменить на готовый worker (Celery/Temporal), но **handle_lease логика** доменная

**Код-доказательство:**
```python
# worker.py:57-106
def handle_lease(lease, *, queue, store, client, analyze, ...):
    result = run_fn(lambda: process(event, analyze, store, force=force), task_timeout)
    if result.state == State.DONE or result.skipped:
        queue.ack(lease.id, lease.token)
        return "ack"
    except Backpressure:
        queue.defer(lease.id, lease.token, delay=backpressure_delay)
        ...
    except Exception as err:
        outcome = queue.nack(...)
        if outcome == "dead_letter":
            _drive_to_dead_letter(store, event.delivery_id)
            notify_failure(client, event, reason, ...)
```

**Причины сохранения:**
- Обработка `Backpressure` отдельно от Exception
- `_drive_to_dead_letter` + `notify_failure` при DLQ
- Release claim при таймауте

**Замена частичная:** цикл `run_forever` → готовый worker, но `handle_lease` сохранить как task handler.

---

#### 2.9 `ingress.py` (64 строки) — **обработка webhook**

**Незаменяемо:** доменная логика приёма

**Код-доказательство:**
```python
# ingress.py:23-64
def handle_webhook(raw, headers, *, secret, store, schedule, enrich):
    if not verify_signature(secret, raw, h.get("x-hub-signature-256")):
        return 401
    ...
    events = parse_events(event_type, delivery_id, payload)
    events = enrich(events)  # head_sha enrichment
    for event in events:
        if store.record_received(event):  # dedup
            schedule(event)
    ...
```

**Причины сохранения:**
- Dedup по `delivery_id` (СТ-2)
- Обогащение `head_sha` ДО записи (business_key зависит от sha)
- HTTP-статусы 400/401 вместо 500 (иначе GitHub бесконечно ретраит)

---

#### 2.10 `app.py` (68 строк) — **FastAPI обвязка**

**Замена любая:** фреймворк агностик, можно перенести на другой (FastAPI/Flask/aiohttp)

---

#### 2.11 `token.py` (95 строк) — **GitHub JWT → installation token**

**Условно заменяемо:** можно использовать готовые библиотеки, но кэш доменный

**Код-доказательство:**
```python
# token.py реализует App JWT → installation token с кэшем
```

**Замена частичная:** криптография → `PyJWT`/`cryptography`, но логику кэша сохранить.

---

#### 2.12 `sentry_setup.py` (156 строк) — **Sentry интеграция**

**Замена любая:** фреймворк агностик

---

## Сводная таблица

| Модуль | Строк | Заменяемость | На что заменить | Сохраняем ли |
|--------|-------|--------------|------------------|--------------|
| `queue.py` | 207 | Высокая | RabbitMQ / Redis Streams / SQS | ❌ Нет |
| `gateway.py` | 199 | Высокая | litellm-proxy / Kong / Azure APIM | ❌ Нет |
| `github_client.py` | 146 | Средняя¹ | PyGithub (транспорт), upsert сохранить | ⚠️ Частично |
| `state.py` | 294 | Низкая | — | ✅ Да |
| `sweeper_adapter.py` | 150 | Низкая | — | ✅ Да |
| `sweeper.py` | 121 | Низкая | — | ✅ Да |
| `worker.py` | 174 | Средняя² | Celery/Temporal, handle_lease сохранить | ⚠️ Частично |
| `webhook.py` | 83 | Низкая | — | ✅ Да |
| `sentry_setup.py` | 156 | Любая | Sentry SDK другой | 🔄 Вариант |
| `token.py` | 95 | Средняя³ | PyJWT, кэш сохранить | ⚠️ Частично |
| `supervisor.py` | 80 | Низкая | — | ✅ Да |
| `metrics.py` | 60 | Высокая | prometheus_client | ❌ Нет |
| `app.py` | 68 | Любая | Flask/aiohttp | 🔄 Вариант |
| `ingress.py` | 64 | Низкая | — | ✅ Да |
| `autoscale.py` | 28 | Высокая | KEDA / HPA | ❌ Нет |
| `notifier.py` | 41 | Низкая | — | ✅ Да |
| `security.py` | 21 | Низкая⁴ | Уже stdlib | ✅ Оставить |
| `logging_setup.py` | 22 | Любая | structlog | 🔄 Вариант |
| `analyze_adapter.py` | 98 | Низкая | — | ✅ Да |
| `sweeper_runner.py` | 85 | Любая | systemd/cron | 🔄 Вариант |
| **ИТОГО** | **2198** | | | |

**Примечания:**
1. `github_client.py`: транспорт можно заменить на PyGithub, но **upsert по маркеру** — доменная логика
2. `worker.py`: цикл → готовый worker, но **handle_lease** (backpressure vs DLQ разделение) доменная
3. `token.py`: криптография → PyJWT, но **процессный кэш** доменный (при нескольких воркерах — особенность)
4. `security.py`: уже использует stdlib `hmac`, оставляем как есть

---

## Итоговые оценки

### Заменяемо (~515 строк, 23%)

```
queue.py (207)          → RabbitMQ / Redis Streams
gateway.py (199)        → litellm-proxy / API Gateway
metrics.py (60)         → prometheus_client
autoscale.py (28)        → KEDA / HPA
security.py (21)        → уже stdlib (оставить)
```

### Доменное (~903 строки, 41%)

```
state.py (294)          → машина состояний, CAS, try_claim, reconcile
supervisor.py (80)      → one-pass логика, already_done, force
notifier.py (41)        → форматирование провал-коммента
webhook.py (83)         → GitHub webhook parsing, enrichment
sweeper.py (121)        → reconciliation логика
sweeper_adapter.py (150) → порты свипера, store vs GitHub verify
ingress.py (64)         → webhook приём, dedup, head_sha enrichment
```

### Условно заменяемо (~515 строк, 23%)

```
github_client.py (146)  → транспорт PyGithub, upsert сохранить
worker.py (174)         → готовый worker, handle_lease сохранить
token.py (95)           → PyJWT, кэш сохранить
```

### Инфраструктурная обвязка (~265 строк, 12%)

```
app.py, logging_setup.py, sentry_setup.py, sweeper_runner.py, analyze_adapter.py
```

---

## Риски при замене

### Потеря гарантий

| Гарантия (СТ) | При замене | Риск |
|----------------|------------|------|
| **СТ-6** (at-least-once) | RabbitMQ | Сообщения теряются при crash брокера без persistent queue |
| **СТ-7** (partition fairness) | SQS | MessageGroupId шардирование по repo — возможно голодание |
| **СТ-9** (poison-guard DLQ) | Redis Streams | Нет native DLQ — реализовывать вручную |
| **СТ-22** (circuit breaker) | litellm-proxy | Backpressure при открытых цепях — нужна retry policy |
| **СТ-16** (идемпотентность) | Любая очередь | dedup на воркере — должна сохраниться логика `already_done` |
| **СТ-25** (upsert коммента) | PyGithub | Upsert по маркеру — надо сохранить логику `_matching_comments` |

### Неявные зависимости

- **`queue.defer`** (backpressure) используется воркером для откладывания без счёта к DLQ. У готовых брокеров может не быть аналога.
- **`state.Backpressure`** исключение ловится воркером отдельно от обычных сбоев. При замене gateway нужно сохранить это различие.
- **Процессный кэш токена** (`token.py`) — при нескольких воркерах кэш не общий, больше вызовов GitHub (корректно, но неоптимально).

---

## Требуемые действия

### 1. Подтвердить/опровергнуть гипотезу

Провести детальный технический анализ:
- Построить матрицу «Свойство → Готовый инструмент»
- Проверить наличие аналогов у:
  - `queue.defer` (backpressure без счёта к DLQ)
  - `partition_service` (fairness между репозиториями)
  - `try_claim` (атомарный захват бизнес-ключа)

### 2. Оценить стоимость миграции

Для каждого заменяемого компонента:
- Трудоёмкость замены (часы)
- Риски потери гарантий
- Тестирование (какие СТ проверить)
- Rollback-план

### 3. Приоритизация

Ранжировать замену по:
- **Выгоде:** сокращение кода / упрощение поддержки
- **Риску:** вероятность потери гарантий
- **Трудоёмкости:** время на миграцию

---

## Критерии завершения

1. **Матрица заменяемости** — для каждого модуля:
   - Что реализует
   - На что можно заменить
   - Какие гарантии сохраняются / теряются
   - Код-доказательства

2. **Приоритизированный список** — что заменять первым:
   - Высокая выгода / низкий риск → начать
   - Низкая выгода / высокий риск → отложить

3. **План миграции** — для топ-3 приоритетов:
   - Шаги замены
   - Тестирование (какие СТ проверить)
   - Rollback-план

---

## Следующий шаг

`/fnr-concept sa_documentation/FNR/FNR_1/task.md` — генерация концептов решений на основе этой ревизии.

Ожидаемые концепты:
- **Concept A:** Минимальная миграция (только queue + metrics)
- **Concept B:** Средняя миграция (queue + metrics + gateway + autoscale)
- **Concept C:** Полная миграция (всё заменяемое)
- **Concept D:** Сохранение status quo (докрутать текущее решение)

---

## Артефакты

- `self-hosted/reliability/README.md` — описание слоя reliability/
- `self-hosted/SYSTEM-REQUIREMENTS.md` — системные требования (СТ)
- `self-hosted/ARCHITECTURE.md` — диаграммы и плейбук
