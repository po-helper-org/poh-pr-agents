# FNR-1: Концепты решений — ревизия reliability/

**Дата:** 2026-07-23
**Задача:** Анализ заменяемости кастомного слоя reliability/ (~2200 строк Python) на готовые инструменты
**Базовый документ:** [`task.md`](task.md)

---

## Executive Summary

Проведена ревизия self-hosted слоя `reliability/` (2198 строк Python, stdlib-only). Гипотеза подтверждена частично: **23% кода** (~515 строк) — инфраструктурная логика, которую можно заменить готовыми инструментами без потери доменных гарантий. **41% кода** (~903 строки) — доменная бизнес-логика, которую следует сохранить.

| Категория | Строк | % | Рекомендация |
|-----------|-------|---|--------------|
| Заменяемое (очередь, gateway, метрики, автоскейл) | ~515 | 23% | Заменить на готовые инструменты |
| Доменное (state, supervisor, sweeper, webhook, notifier) | ~903 | 41% | Сохранить как есть |
| Условно заменяемое (github_client, worker, token) | ~515 | 23% | Частичная замена |
| Инфраструктурная обвязка (app.py, sentry, logging) | ~265 | 12% | Опционально |

**Ключевое ограничение:** Система работает на одном узле Dokploy с shared SQLite. Миграция на распределённые инструменты (RabbitMQ, Redis) оправдана только при масштабировании на несколько узлов.

---

## Concept A: Минимальная миграция (низковисящие плоды)

> **Категория:** Прагматичное
> **Уровень риска:** Низкий
> **Усилия:** 2-4 дня

### Суть

Замена только тех компонентов, где:
1. Готовый инструмент даёт **точечный функциональный эквивалент**
2. Миграция не требует переписывания доменной логики
3. Гарантии (СТ) сохраняются без дополнительных костылей

Заменяем: `metrics.py` → `prometheus_client`, `security.py` оставить (уже stdlib), `autoscale.py` → KEDA (если Kubernetes).

### Изменяемые компоненты

| Модуль | Действие | Replacement | Сохраняемые гарантии |
|--------|----------|-------------|---------------------|
| `metrics.py` (60) | Заменить | `prometheus_client` | Полностью — те же метрики |
| `autoscale.py` (28) | Заменить | KEDA / HPA | Age pressure → KEDA triggers |
| `security.py` (21) | Оставить | Уже stdlib `hmac` | — |

**Всего строк к удалению:** ~60 (metrics) + ~28 (autoscale) = **88 строк (4%)**

### Плюсы

- **Минимальный риск:** изменения локальны, не затрагивают core path
- **Быстрый выигрыш:** меньше кода, стандартные метрики (histogram/percentile)
- **Обратимость:** откат прост — вернуть старые модули

### Минусы

- **Ограниченная выгода:** 88 строк из 2200 — менее 4% сокращения
- **Очередь и gateway остаются:** основные "домашние" компоненты нетронуты

### План миграции

1. `metrics.py` → `prometheus_client`
   - Заменить `_counter` на `Counter`
   - Добавить `Histogram` для latency
   - `/metrics` endpoint через `start_http_server`
2. `autoscale.py` → KEDA scaler
   - ScaledObject с RabbitMQ/Redis trigger (или custom metric)
   - Если не Kubernetes — оставить как есть

### Усилия

- **Разработка:** 8-12 часов
- **Тестирование:** 4-8 часов
- **Deploy:** 2-4 часа

### Риски

- `prometheus_client` добавляет зависимость (стабильная, широко используемая)
- KEDA требует Kubernetes (если не K8s — не применимо)

---

## Concept B: Прагматичная миграция (очередь + gateway)

> **Категория:** Прагматичное
> **Уровень риска:** Средний
> **Усилия:** 1-2 недели

### Суть

Замена двух основных инфраструктурных компонентов:
1. **Очередь:** SQLite → RabbitMQ / Redis Streams
2. **Gateway:** кастомный → litellm-proxy / Kong

Это даёт максимальную выгоду при управляемом риске. Доменная логика (`state.py`, `supervisor.py`, `sweeper.py`) сохраняется.

### Изменяемые компоненты

| Модуль | Действие | Replacement | Миграция интерфейса |
|--------|----------|-------------|-------------------|
| `queue.py` (207) | Заменить | RabbitMQ / Redis Streams | `enqueue/lease/ack/nack/defer` → adapter |
| `gateway.py` (199) | Заменить | litellm-proxy / Kong | Exception types → adapter |
| `metrics.py` (60) | Заменить | `prometheus_client` | — |
| `autoscale.py` (28) | Заменить | KEDA / HPA | — |

**Всего строк к удалению:** ~494 (22%)

### Архитектура после миграции

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         reliability/ слой (после)                      │
├─────────────────────────────────────────────────────────────────────────┤
│  ingress.py → RabbitMQ ← worker.py                                     │
│                  ↓                                                      │
│              state.py (сохранён)                                       │
│                  ↓                                                      │
│           litellm-proxy → analyze_adapter                               │
│                  ↓                                                      │
│            notifier.py ──→ github_client.py                            │
│                                                                          │
│  Периодический: sweeper.py ← sweeper_adapter.py                         │
│  Метрики: prometheus_client (замена metrics.py)                         │
│  Автоскейл: KEDA (замена autoscale.py)                                  │
└─────────────────────────────────────────────────────────────────────────┘
```

### Плюсы

- **Максимальная выгода:** 494 строки (22%) заменяются на боевые инструменты
- **Бо́льшая надёжность:** RabbitMQ/Redis — battle-tested,七年多的生产经验
- **Масштабируемость:** растёт на несколько узлов (shared SQLite не limiting factor)
- **Наблюдаемость:** готовые metrics/prometheus integration

### Минусы

- **Сложнее миграция:** нужен адаптер под очередь/gateway
- **Новая инфраструктура:** RabbitMQ/Redis → deploy/operate
- **Риск потери нюансов:** `queue.defer` (backpressure), `partition_service` (fairness)

### Критические нюансы для сохранения гарантий

| Нюанс | Проблема | Решение |
|-------|----------|---------|
| `queue.defer` (отложить без счёта к DLQ) | RabbitMQ 没有 native defer | Re-queue с delay via `x-delay` plugin или dead-letter + retry |
| `partition_service` (fairness) | SQS MessageGroupId не гарантирует строгую честность | RabbitMQ consistent hashing + consumers per partition |
| `Backpressure` vs `GatewayUnavailable` | Litelm-proxy не различает | Adapter мапит response codes → наши exception types |
| `state.Backpressure` (в воркере) | Зависит от gateway exception | Adapter должен бросить `Backpressure` при rate limit |

### План миграции

#### Фаза 1: Очередь (5-8 дней)

1. Выбор технологии:
   - **RabbitMQ:** native DLQ, TTL/dead-letter exchange, acknowledgements, consistent hashing
   - **Redis Streams:** XREADGROUP, XACK, XPENDING — но нет native DLQ
   - **Рекомендация:** RabbitMQ (если нужен горизонтальный scale)

2. Adapter `queue_adapter.py`:
   ```python
   class RabbitMQQueue:
       def __init__(self, url: str, ...):
           self._conn = pika.BlockingConnection(url)
           self._channel = self._conn.channel()
           # Declare queues: main + DLQ
           # Setup dead-letter exchange

       def enqueue(self, payload: dict, partition: str, *, delay: float = 0) -> int:
           # Publish to partition-specific queue
           # delay via x-delay plugin or DLQ + TTL

       def lease(self, *, visibility_timeout: float, max_attempts: int) -> Optional[Lease]:
           # Basic.get (single consumer per partition)
           # Set delivery_tag = token

       def ack(self, message_id: int, token: str) -> bool:
           # Basic.ack(delivery_tag)

       def nack(self, message_id: int, token: str, ...) -> str:
           # Basic.reject/nack(requeue=True)
   ```

3. Замена в `worker.py`:
   ```python
   # Было: queue = DurableQueue("queue.db")
   # Станет: queue = RabbitMQQueue(os.environ["RABBITMQ_URL"])
   ```

4. Тестирование:
   - Chaos: kill worker — сообщение перехвачено другим consumer
   - DLQ: max_attempts → dead-letter (подтверждение в RabbitMQ UI)
   - Backpressure: defer → сообщение возвращается без increment attempts

#### Фаза 2: Gateway (3-5 дней)

1. Выбор технологии:
   - **litellm-proxy:** встроенный circuit breaker, rate limiting, failover
   - **Kong / Azure APIM:** enterprise-ready, но overhead

2. Adapter `gateway_adapter.py`:
   ```python
   class LiteLLMProxyAdapter:
       def __init__(self, proxy_url: str, ...):
           self._proxy_url = proxy_url

       def invoke(self, event: Event) -> None:
           response = requests.post(
               f"{self._proxy_url}/v1/chat/completions",
               json=build_prompt(event),
               timeout=ATTEMPT_TIMEOUT
           )
           if response.status_code == 429:
               raise RateLimited()  # Backpressure
           if response.status_code >= 500:
               raise GatewayUnavailable()  # Retry
           # Circuit open → litellm-proxy вернёт 503 fast fail
   ```

3. Замена в `worker.py`:
   ```python
   # Было: gateway = Gateway(...)
   # Станет: gateway = LiteLLMProxyAdapter(os.environ["LITELLM_PROXY_URL"])
   ```

4. Тестирование:
   - Circuit open → `GatewayCircuitOpen` (но adapter должен бросить `Backpressure`?)
   - Rate limit → `RateLimited` (не засчитывается к DLQ)
   - Failover → второй ключ (если >1 key)

#### Фаза 3: Deploy и rollback (2-3 дня)

1. Стратегия: blue-green deployment
   - Новые воркеры читают из RabbitMQ, старые — из SQLite
   - Bridge: consumer group читает из SQLite → публикует в RabbitMQ
   - Переключение webhook: new ingress → RabbitMQ

2. Rollback-план:
   - Switch ingress → SQLite queue
   - Воркеры auto-reconnect к SQLite

### Усилия

- **Разработка:** 40-60 часов
- **Тестирование:** 20-30 часов
- **Deploy:** 8-12 часов

### Риски

| Риск | Вероятность | Влияние | Митигация |
|------|-------------|---------|-----------|
| Потеря сообщений при crash брокера | Средняя | Высокое | Persistent queue + confirms |
| Fairness нарушена | Низкая | Среднее | Мониторинг `queue_depth` per partition |
| Backpressure не работает | Средняя | Высокое | Детальный тест `defer` семантики |
| Deploy outage | Низкая | Высокое | Blue-green + bridge |

---

## Concept C: Полная замена инфраструктуры

> **Категория:** Правильное (но дорогое)
> **Уровень риска:** Высокий
> **Усилия:** 3-4 недели

### Суть

Замена всего заменяемого (queue, gateway, metrics, autoscale) **плюс** условно заменяемого (github_client, worker, token) на готовые решения. Цель — минимизировать собственный код, но ценой глубокой миграции.

### Изменяемые компоненты

| Модуль | Действие | Replacement | Сохраняемое |
|--------|----------|-------------|-------------|
| `queue.py` (207) | Заменить | RabbitMQ | — |
| `gateway.py` (199) | Заменить | litellm-proxy | — |
| `metrics.py` (60) | Заменить | `prometheus_client` | — |
| `autoscale.py` (28) | Заменить | KEDA | — |
| `github_client.py` (146) | Частично | PyGithub (транспорт) | Upsert-логика |
| `worker.py` (174) | Частично | Celery/Temporal | `handle_lease` как task handler |
| `token.py` (95) | Частично | PyJWT | Кэш (redis/shared) |
| `security.py` (21) | Оставить | stdlib | — |

**Всего строк к удалению:** ~689 (31%) + частичная замена ~415 (19%) = **1104 строки (50%)**

### Архитектура после миграции

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    reliability/ слой (полная замена)                     │
├─────────────────────────────────────────────────────────────────────────┤
│  ingress.py → Celery broker (RabbitMQ) ← Celery worker                    │
│                  ↓                                                       │
│              state.py (сохранён)                                          │
│                  ↓                                                       │
│           litellm-proxy → pr-agent                                        │
│                  ↓                                                       │
│            notifier.py ──→ PyGithub (upsert сохранён)                    │
│                                                                          │
│  Периодический: sweeper.py (сохранён)                                     │
│  Метрики: prometheus_client                                               │
│  Автоскейл: KEDA                                                           │
│  Worker: Celery beat / Temporal workflow                                  │
└─────────────────────────────────────────────────────────────────────────┘
```

### Плюсы

- **Максимальное сокращение кода:** 50% слоя reliability/
- **Стандартные компоненты:** Celery, Temporal, PyGithub — battle-tested
- **Богаче возможности:** Celery has retries, scheduling, workflows
- **Сообщество:** готовые ответы на common issues

### Минусы

- **Сложная миграция:** несколько технологий, точки интеграции
- **Тяжёлая зависимость:** Celery/Temporal — heavy frameworks
- **Риск переписывания логики:** `handle_lease` (backpressure vs DLQ) сложно мапнуть на Celery task
- **Не все выгоды:** upsert-логика github_client, sweeper — доменное, не уходит

### План миграции

#### Фаза 1: Worker → Celery (7-10 дней)

1. Определить Celery task:
   ```python
   @app.task(bind=True, max_retries=5)
   def process_pr_event(self, event_dict: dict):
       event = event_from_dict(event_dict)
       try:
           result = supervisor.process(event, analyze, store)
           if result.state == State.DONE:
               store.mark_done(...)
               client.publish_comment(...)  # upsert сохранён
       except Backpressure:
           # Celery retry с exponential backoff
           raise self.retry(countdown=BACKPRESSURE_DELAY, max_retries=None)
       except Exception:
           # Dead-letter
           notify_failure(client, event, ...)
   ```

2. Bridge `handle_lease` → Celery:
   - `lease` → Celery берёт из broker
   - `ack` → task succeeded
   - `nack` → task retry/failed
   - `defer` → retry с countdown (НЕ max_retries)

3. Замена в `worker.py`:
   ```python
   # Было: run_forever(lambda: DurableQueue(...))
   # Станет: celery worker --loglevel=info
   ```

#### Фаза 2: GitHub Client → PyGithub (3-5 дней)

1. Adapter `github_client_adapter.py`:
   ```python
   class GithubClient:
       def __init__(self, app_id: int, private_key: str):
           self._github = Github(app_auth=AppAuth(...))

       def upsert_comment(self, repo: str, number: int, marker: str, body: str) -> None:
           # Сохраняем upsert-логику из оригинала:
           # 1. Найти комментарий с marker
           # 2. Если есть → PATCH
           # 3. Если нет → POST
           # 4. Удалить дубли (self-heal)
           pr = self._github.get_repo(repo).get_pull(number)
           matches = [c for c in pr.get_issue_comments() if marker in c.body]
           if not matches:
               pr.create_issue_comment(body + "\n\n" + marker)
               return
           matches[0].edit(body + "\n\n" + marker)
           for extra in matches[1:]:
               extra.delete()
   ```

#### Фаза 3: Token → PyJWT (1-2 дня)

1. Adapter `token_adapter.py`:
   ```python
   from jwt import JWT

   class TokenCache:
       def __init__(self, app_id: int, private_key: str, redis_url: str):
           self._jwt = JWT()
           self._redis = redis.from_url(redis_url)

       def get_installation_token(self, installation_id: int) -> str:
           cache_key = f"token:{installation_id}"
           cached = self._redis.get(cache_key)
           if cached:
               return cached
           token = self._jwt.encode(...)  # PyJWT
           self._redis.setex(cache_key, 3600, token)
           return token
   ```

### Усилия

- **Разработка:** 80-120 часов
- **Тестирование:** 40-60 часов
- **Deploy:** 16-24 часа

### Риски

| Риск | Вероятность | Влияние | Митигация |
|------|-------------|---------|-----------|
| Celery не мапит `defer` семантику | Высокая | Высокое | Кастомный retry policy |
| Celery task execution order не определён | Средняя | Среднее | Мониторинг `processing_latency` |
| PyGithub upsert race condition | Низкая | Среднее | Test + pagination |
| Redis cache cold start на нескольких воркерах | Средняя | Низкое | Warm-up скрипт |

---

## Concept D: Эволюционный подход (сохранить и улучшить)

> **Категория:** "Не трогай"
> **Уровень риска:** Минимальный
> **Усилия:** 1-2 недели

### Суть

**Не заменять** готовыми инструментами, а **улучшить** существующую реализацию:
- Добавить недостающие фичи (retention partition_service, консолидация sweeper-stale ↔ queue-redelivery)
- Усилить тестирование (интеграционные тесты, chaos engineering)
- Подготовиться к будущей миграции (abstract interfaces)

Этот концепт — "status quo с выгодой": существующий код работает, гарантии (СТ) выполняются, усилия лучше потратить на полировку, чем на рискованную миграцию.

### Улучшаемые компоненты

| Модуль | Улучшение | Выгода |
|--------|-----------|--------|
| `queue.py` | Retention partition_service (растёт по репо) | Не раздувается БД |
| `queue.py` | Консолидация sweeper-stale ↔ queue-redelivery | Меньше дублирования логики |
| `gateway.py` | Shared rate limit (Redis) при N воркерах | Эффективнее использование лимита Z.AI |
| `state.py` | Миграция на Postgres (опционально) | Готовность к scale |
| `worker.py` | Улучшенная обработка `Backpressure` vs Exception | Чётче разделение retry vs DLQ |

### Плюсы

- **Нулевой риск миграции:** код уже работает
- **Понятная выгода:** конкретные улучшения вместо абстрактной "замены"
- **Готовность к будущему:** abstract interfaces → лёгкая миграция позже
- **Сохранение контроля:** полная уверенность в семантике

### Минусы

- **Больше кода для поддержки:** 2200 строк остаются
- **Less "standard":** не пользуемся преимуществами боевых инструментов
- **Технический долг:** eventual migration всё равно может понадобиться

### План улучшения

#### Фаза 1: Retention и консолидация (3-5 дней)

1. `queue.py`: добавить `prune_partition_service`
   ```python
   def prune_partition_service(self, older_than_days: int = 30) -> int:
       """Удалить старые partition records (retention)."""
       now = self._clock()
       cutoff = now - (older_than_days * 86400)
       with self._lock:
           cur = self._db.execute(
               "DELETE FROM partition_service WHERE last_served < ?",
               (cutoff,)
           )
           self._db.commit()
           return cur.rowcount
   ```

2. Консолидация sweeper-stale ↔ queue-redelivery:
   - Sweeper не должен дублировать visibility-timeout семантику
   - Вынести общую логику в `queue.get_stale_leases()`

#### Фаза 2: Shared rate limit (3-4 дня)

1. `gateway.py`: Redis-based token bucket
   ```python
   class RedisTokenBucket:
       def __init__(self, redis_url: str, rate: float, capacity: float):
           self._redis = redis.from_url(redis_url)
           self._rate = rate
           self._capacity = capacity

       def try_acquire(self, key: str) -> bool:
           # Atomic Lua script в Redis
           # Если tokens > 0 → decrement и return True
           # Иначе → False
   ```

2. Воркеры используют общий limiter → RPS ≈ лимит Z.AI (не N×rate)

#### Фаза 3: Abstract interfaces (2-3 дня)

1. Определить interfaces для future migration:
   ```python
   # reliability/interfaces.py
   from abc import ABC, abstractmethod

   class Queue(ABC):
       @abstractmethod
       def enqueue(self, payload: dict, partition: str, *, delay: float = 0) -> int: ...

       @abstractmethod
       def lease(self, *, visibility_timeout: float, max_attempts: int) -> Optional[Lease]: ...

       @abstractmethod
       def ack(self, message_id: int, token: str) -> bool: ...

       @abstractmethod
       def nack(self, message_id: int, token: str, ...) -> str: ...

   class Gateway(ABC):
       @abstractmethod
       def invoke(self, event: Event) -> None: ...
   ```

2. Реализация `DurableQueue` implements `Queue`
3. Будущая миграция: реализация `RabbitMQQueue` implements `Queue` → drop-in replacement

### Усилия

- **Разработка:** 30-40 часов
- **Тестирование:** 10-20 часов
- **Deploy:** 4-8 часов

### Риски

- **Низкий риск:** изменения локальны, хорошо тестируются
- Единственный риск: улучшения не оправдывают усилий (но это измеримо)

---

## Concept E: Гибридное решение (очередь → RabbitMQ, остальное → как есть)

> **Категория:** Прагматичное
> **Уровень риска:** Средний
> **Усилия:** 1-1.5 недели

### Суть

Компромисс между B и D:
- Заменить **только очередь** (самый большой компонент, 207 строк)
- Остальное оставить (gateway, metrics, autoscale)
- Добавить abstract interfaces для future gateway migration

Это даёт **масштабируемость** (несколько узлов) без полной переписи gateway/worker.

### Изменяемые компоненты

| Модуль | Действие | Replacement |
|--------|----------|-------------|
| `queue.py` (207) | Заменить | RabbitMQ / Redis Streams |
| `metrics.py` (60) | Опционально | `prometheus_client` (или оставить) |
| Остальное | Оставить | — |

**Всего строк к удалению:** ~207 (9.4%) + опционально ~60 = **267 (12%)**

### Плюсы

- **Масштабируемость:** shared queue → несколько узлов
- **Управляемый риск:** только очередь меняется
- **Быстрее:** 1-1.5 недели vs 3-4 недели (Concept C)
- **Gateway остаётся:** custom logic сохраняется (Backpressure vs GatewayUnavailable)

### Минусы

- **Gateway всё ещё custom:** 199 строк остаются
- **Metrics не улучшены:** остаётся своя реализация
- **Autoscale не интегрирован:** если K8s — нужна KEDA

### План миграции

Аналогичен Concept B Фаза 1 (очередь), но без Фазы 2 (gateway).

### Усилия

- **Разработка:** 30-40 часов (только queue adapter)
- **Тестирование:** 15-20 часов
- **Deploy:** 8-12 часов

### Риски

- Те же, что в Concept B для очереди
- Gateway остаётся процессным — при scale возможно inefficiency rate limit

---

## Сравнительная таблица концептов

| Концепт | Усилия | Риск | Удаление строк | Выгода | Рекомендация |
|---------|--------|-----|----------------|--------|--------------|
| **A: Минимальная** | 2-4 дня | Низкий | 88 (4%) | Стандартные метрики | Если нет планов на scale |
| **B: Очередь+Gateway** | 1-2 недели | Средний | 494 (22%) | Max выгода / риск | Если нужен scale |
| **C: Полная замена** | 3-4 недели | Высокий | 1104 (50%) | Минимум кода | Если есть ресурсы |
| **D: Эволюционный** | 1-2 недели | Минимальный | 0 | Улучшения | Если не хочется миграции |
| **E: Гибрид** | 1-1.5 недели | Средний | 207-267 (9-12%) | Scale быстрее | Если нужна только очередь |

---

## Рекомендация

**Concept B (Прагматичная миграция: очередь + gateway)** — оптимальный баланс выгоды и риска.

**Почему:**
1. **22% кода** заменяется на боевые инструменты
2. **Масштабируемость:** несколько узлов → shared queue
3. **Управляемый риск:** адаптеры → проверяемые границы
4. **Сохранение домена:** state, supervisor, sweeper остаются

**Когда выбрать другой:**
- **Concept A** — если нет планов на scale, текущее решение работает
- **Concept C** — если есть ресурсы и желание минимизировать код
- **Concept D** — если migration risk недопустим
- **Concept E** — если нужна только масштабируемость очереди

---

## Следующие шаги

1. **`/fnr-debate sa_documentation/FNR/FNR_1/concept.md`** — архитектурные дебаты (Architect vs Devil's Advocate) для выбора финального концепта
2. **`/fnr-system-requirements sa_documentation/FNR/FNR_1/concept.md`** — генерация системных требований на основе выбранного концепта

---

## Артефакты

- [`task.md`](task.md) — ревизия reliability/
- [`../ARCHITECTURE.md`](../ARCHITECTURE.md) — диаграммы и плейбук
- [`../SYSTEM-REQUIREMENTS.md`](../SYSTEM-REQUIREMENTS.md) — системные требования
- [`repomix-output.xml`](../repomix-output.xml) — полный код базы

---

# Архитектурные дебаты — Architect vs Devil's Advocate

**Дата:** 2026-07-23
**Формат:** 3 раунда структурированных дебатов
**Выбранный концепт для защиты:** Concept B (Прагматичная миграция: очередь + gateway)

---

## Раунд 1: Защита

### Архитектор

Я выбираю **Concept B** — прагматичную миграцию очереди и gateway. Это оптимальный баланс выгоды и риска для текущей ситуации.

**Аргумент 1: Масштабируемость — критический limitation**

Текущая архитектура работает на одном узле Dokploy с shared SQLite. Из `queue.py:36-43`:
```python
self._db = sqlite3.connect(path, check_same_thread=False)
self._db.execute("PRAGMA journal_mode=WAL")
```
SQLite с WAL — **одноузловое** решение. При масштабировании на несколько воркеров (что уже планируется по `SCALE-PLAN.md`) возникнет race condition. Хотя `threading.Lock` сериализует внутри процесса, при нескольких процессах воркера SELECT→UPDATE будет TOCTOU — две реплики лизнут одно сообщение (см. комментарий в `queue.py:100-110`).

**Аргумент 2: 22% кода — инфраструктура, заменяемая без потери СТ**

- `queue.py` (207 строк) — RabbitMQ/Redis Streams реализуют те же гарантии (at-least-once, DLQ, acknowledgements)
- `gateway.py` (199 строк) — litellm-proxy имеет встроенный circuit breaker и rate limiting
- `metrics.py` (60 строк) — prometheus_client даёт histogram/percentile

Доменная логика сохраняется: `state.py`, `supervisor.py`, `sweeper.py`, `notifier.py` — это 41% кода, который НЕ трогаем.

**Аргумент 3: Готовые инструменты battle-tested**

RabbitMQ — 7+ лет в продакшене, native DLQ, consistent hashing для fairness. Litellm-proxy — используется тысячами команд, проверенные паттерны circuit breaker.

**Аргумент 4: Управляемый риск через адаптеры**

Мы НЕ переписываем логику — мы создаём тонкие адаптеры:
```python
class RabbitMQQueue:
    def enqueue(self, payload: dict, partition: str, *, delay: float = 0) -> int:
        # Publish to partition-specific queue
    def lease(self, *, visibility_timeout: float, max_attempts: int) -> Optional[Lease]:
        # Basic.get + delivery_tag = token
```

Interface сохраняется — drop-in replacement для `worker.py`.

---

### Адвокат Дьявола

Твоя позиция звучит разумно, но я вижу критические риски.

**Атака 1: Потеря семантики `defer` — скрытый баг**

Внимание на `worker.py:70-76` и `queue.py:147-158`:
```python
# worker.py
except Backpressure:
    queue.defer(lease.id, lease.token, delay=backpressure_delay)
    # ...
    return "deferred"
```
```python
# queue.py:147-158 — defer ОТКАТЫВАЕТ attempts++
def defer(self, message_id: int, token: str, *, delay: float) -> str:
    self._db.execute(
        "UPDATE messages SET attempts=MAX(0, attempts-1), leased_until=NULL, "
        "lease_token=NULL, available_at=? WHERE id=?",
        (now + delay, message_id),
    )
```

**Критическое свойство:** defer откладывает сообщение **без счёта к DLQ**. Это для rate-limit — иначе один prolonged outage Z.AI отправил бы весь бэклог в dead-letter.

**У RabbitMQ НЕТ прямого аналога defer.** Re-queue с `x-delayed-message` plugin — это всё равно попытка выдачи. Если воркер откладывает из-за backpressure 10 раз — он зачтётся к max_attempts? Если да — это баг, сломанный паттерн backpressure.

**Атака 2: Partition fairness — не гарантируется**

Текущая реализация `queue.py:73-82` использует `partition_service` таблицу:
```python
SELECT m.partition AS p, MIN(m.id) AS mid
FROM messages m
LEFT JOIN partition_service ps ON ps.partition = m.partition
...
ORDER BY COALESCE(ps.last_served, 0) ASC, mid ASC
```

Это даёт **строгую честность**: "один тяжёлый репо не голодит остальные" (СТ-7). При RabbitMQ consistent hashing или SQS MessageGroupId **нет строгой гарантии** — возможно голодание маленьких репо.

**Атака 3: Gateway semantics — Backpressure vs Unavailable**

В `gateway.py:140-150` критично различать:
```python
if not attempted:
    # Все цепи разомкнуты — системный простой
    raise GatewayCircuitOpen(...)  # Backpressure → defer
# ...
# Реальный сбой попытки
raise GatewayUnavailable(...)  # → nack + DLQ
```

Литтелм-прокси **не различает** эти случаи. Он вернёт 503 и при circuit open, и при сбое провайдера. Твой adapter должен будет мапить response codes — но как отличить "все цепи разомкнуты" от "все провайдеры сбоили"? Логика состояния breaker **процессная** — при нескольких воркерах каждый видит своё состояние.

**Атака 4: Deploy outage — blue-green bridge сложнее

Ты предлагаешь "blue-green deployment" с bridge из SQLite в RabbitMQ. Но:
- Webhook направляется на new ingress → RabbitMQ
- Старые воркеры читают из SQLite
- Bridge Consumer group читает из SQLite → публикует в RabbitMQ

**Что происходит с的消息 в момент переключения?** Если bridge не успел прочитать из SQLite все сообщения, а новый воркер уже начал читать из RabbitMQ — возможно дублирование или пропуск.

**Атака 5: Усилия недооценены**

Ты пишешь "40-60 часов разработки". Но:
- Adapter для очереди + тестирование chaos scenarios: 20-30 часов
- Adapter для gateway + тестирование circuit/backpressure: 15-20 часов
- Bridge для blue-green deploy: 10-15 часов
- **Минимум 45-65 часов**, ближе к верхней границе.

И это без "unknown unknowns" — например, поведение RabbitMQ при краше брокера.

---

## Раунд 2: Пересмотр

### Архитектор

Действенные атаки. Позволь мне усилить аргументацию и предложить митигации.

**Усиление по Атаке 1 (defer semantics):**

Да, RabbitMQ не имеет прямого defer. Но паттерн известен:
1. **Вариант A:** DLQ + TTL → re-queue. Сообщение идёт в DLQ, но с TTL=delay, после которого автоматический dead-letter exchange возвращает в main queue. DLQ не считается "fail" — это часть backpressure логики.
2. **Вариант B:** Отдельная "delay queue" для каждого partition. Сообщение публикуется в delay queue с TTL, после которой оно маппится в main.

**Но критически:** в текущей реализации `queue.defer` — это **оптимизация**, не базовое требование. Если defer не идеален — worst case, backpressure сообщения偶尔 считаются к DLQ. Это **не катастрофа**, потому что:
- Backpressure происходит при rate limit — временное состояние
- Даже если сообщение уходит в DLQ из-за backpressure — sweeper reconciles его (СТ-29..32)

**Усиление по Атаке 2 (partition fairness):**

Текущая реализация fairness через `partition_service` — это **sweet spot algorithm**, но не строгая гарантия. Предлагаю:
- При RabbitMQ использовать **consistent hashing** с consumers per partition
- Мониторинг `queue_depth` per partition — если одно репо голодает >5min, алёрт
- Альтернатива: SQS с MessageGroupId + FIFO queue — **строгая гарантия** fairness

**Усиление по Атаке 3 (gateway semantics):**

Да, litellm-proxy не различает. Но Adapter может хранить состояние breaker **in-memory per process**:
```python
class LiteLLMProxyAdapter:
    def __init__(self, proxy_url: str):
        self._breaker = CircuitBreaker(...)  # per-process, как сейчас
    def invoke(self, event: Event) -> None:
        if not self._breaker.allow():
            raise GatewayCircuitOpen(...)  # ← semantics preserved
        response = requests.post(f"{self._proxy_url}/v1/chat/completions", ...)
        if response.status_code == 429:
            raise RateLimited()
        if response.status_code >= 500:
            self._breaker.record_failure()
            raise GatewayUnavailable()
```

**Важно:** мы сохраняем per-process breaker, а litellm-proxy используется только как транспорт. Semantics **полностью сохраняются**.

**Усиление по Атаке 4 (deploy outage):**

Ты прав — bridge сложен. Предлагаю более простую стратегию:
1. **Canary deployment**: сначала 10% webhook трафика → new ingress → RabbitMQ, остальные 90% → SQLite
2. **Graceful drain**: остановка ingress со старой очередью, все сообщения прочитываются воркерами
3. **Cutover**: 100% трафика → new

Rollback простой: переключить webhook обратно на старый ingress.

**Пивот — возможно, Concept E лучше:**

Но учитывая твои атаки, я допускаю что **Concept E (Гибрид — только очередь)** может быть лучше:
- Заменяем только queue (самый критичный компонент для scale)
- Gateway остаётся custom — semantics полностью сохранены
- Усилия: 30-40 часов (vs 40-60 для Concept B)
- Риск: ниже (меньше точек интеграции)

---

### Адвокат Дьявола

Усиления принимаются, но вижу новые проблемы.

**Атака на пивот к Concept E:**

Если ты заменяешь только queue, но оставляешь gateway custom — теряется часть выгоды. Gateway остаётся 199 строк, metrics — 60 строк. В итоге:
- Concept E: 207-267 строк (9-12%)
- Concept B: ~494 строк (22%)

**Всего на 15% больше кода удаляется** за вдвое больший риск. Стоит ли?

**Атака на "defer не критично":**

Ты сказал "defer — оптимизация, не базовое требование". Но смотри на `worker.py:70-76`:
```python
except Backpressure:
    queue.defer(lease.id, lease.token, delay=backpressure_delay)
    metrics.incr("backpressure_deferred")
    logger.info("processed: delivery=%s command=%s → deferred (rate limit, %ss)", ...)
    return "deferred"
```

Если defer не работает как задумано — каждый rate limit превращается в nack → attempts растёт → сообщение уходит в DLQ **раньше времени**. Это означает:
- Ложные DLQ при временном throttling Z.AI
- Лишние fail-комменты в PR (нарушение принципа "не молчать")
- Потерю доверия к боту

**Атака на "per-process breaker в adapter":**

Если ты сохраняешь per-process breaker в adapter — зачем тогда litellm-proxy? Ты выносишь транспорт, но логика breaker остаётся твоей. Выгода — только в failover по пулу провайдеров. Но у тебя **один ключ Z.AI** — failover не работает.

**Атака на fairness через SQS:**

SQS FIFO queue с MessageGroupId — это строго fairness, но:
- FIFO queue имеет **limit 300 messages per second** per MessageGroupId. Если одно репо генерирует >300 events/sec — throttling очереди.
- Нет native delay queue — нужен separate SQS для delay или workaround.

**Контр-вопрос к масштабированию:**

Ты говоришь "масштабируемость — критический limitation". Но смотри на реальность:
- Система работает на **одном узле Dokploy**
- SQLite shared между ingress/worker/sweeper
- Нет планов на multi-node deploy в ближайшие 6 месяцев

**Зачем мигрировать на RabbitMQ ради масштабирования, которого нет?** Это premature optimisation.

---

## Раунд 3: Вердикт

### Архитектор (итоговая позиция)

Я признаю твои атаки справедливыми. Пересматриваю позицию:

**Если горизонтальное масштабирование НЕ требуется в ближайшие 6 месяцев:**
- Concept B и E — **premature optimisation**
- Варианты с очередью (RabbitMQ/Redis) добавляют операционную сложность без реальной выгоды
- Defer semantics сложны сохранить на готовых брокерах

**Но если масштабирование планируется через 3-6 месяцев:**
- Concept B оправдан — подготовка к multi-node
- Defer semantics можно реализовать через DLQ+TTL workaround

**Моя рекомендация зависит от временного горизонта:**
- **6+ месяцев без scale:** Concept D (эволюционный подход) или Concept A (минимальная)
- **3-6 месяцев до scale:** Concept E (только очередь, gateway custom)
- **<3 месяцев до scale:** Concept B (полная миграция)

---

### Адвокат Дьявола (итоговая позиция)

Я вижу два резонных пути:

**Путь 1: Concept D (Эволюционный) — если система работает**
- Текущий код покрыт тестами (107 unittest)
- Все гарантии (СТ) реализованы и проверяемы
- Улучшения (retention, shared rate limit, abstract interfaces) дают реальную выгоду без риска
- **Когда:** если нет боли от текущей реализации

**Путь 2: Concept A (Минимальная) — если нужна только наблюдаемость**
- Заменить `metrics.py` → `prometheus_client` — 60 строк, тривиально
- Остальное оставить как есть
- Подготовить abstract interfaces для будущей миграции
- **Когда:** если нужны histogram/percentile метрики, но scale не планируется

**Concept B/E НЕ рекомендую** потому что:
- Defer semantics сложно сохранить идеально
- Fairness гарантии частично теряются
- Операционная сложность (RabbitMQ/Redis) не оправдана без реального scale
- Выгода (9-22% меньше кода) не стоит риска (разрушение backpressure паттерна)

---

## Вердикт

После трёх раундов дебатов стороны пришли к консенсусу:

**Выбранный концепт:** **Concept D (Эволюционный подход)** с элементами Concept A

**Обоснование:**

1. **Система работает на одном узле** — горизонтальное масштабирование не планируется в ближайшие 6 месяцев
2. **Defer semantics — критичный паттерн** — сохранение perfect semantics важнее сокращения строк кода
3. **Проверяемые гарантии (СТ)** — текущая реализация покрыта 107 unittest, все требования трассируемы
4. **Риск/выгода соотношение** — Concept B/E дают 9-22% сокращения кода ценой нарушения backpressure паттерна и частичной потери fairness

**Комбинированный план:**

1. **Concept A (минимальная):** заменить `metrics.py` → `prometheus_client` для histogram/percentile метрик
2. **Concept D (эволюционная):**
   - Добавить retention для `partition_service` (растёт по репо)
   - Консолидировать sweeper-stale ↔ queue-redelivery логику
   - Abstract interfaces (`Queue`, `Gateway`) для future migration
   - Shared rate limit (Redis) для будущих multi-node воркеров

**Усилия:** 30-50 часов (vs 40-60 для Concept B)
**Риск:** Минимальный (локальные изменения, хорошо тестируются)
**Выгода:**
- Улучшенная наблюдаемость (prometheus_client histogram)
- Готовность к будущей миграции (abstract interfaces)
- Конкретные улучшения вместо рискованной замены

**Условие для пересмотра:** Если через 6 месяцев потребуется горизонтальное масштабирование → повторно оценить Concept B/E с учётом накопленного опыта.

---
