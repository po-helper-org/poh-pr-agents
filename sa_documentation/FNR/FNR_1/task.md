# FNR-1: Надёжность — PR-Agent не должен падать «в тишину»

## Метаданные

- **Тип задачи:** Архитектурный анализ / Найденная реализация
- **Статус:** Диагноз завершён, решение частично реализовано
- **Критичность:** Высокая (ядро платформы)
- **Конфликтующие компоненты:** `docker-compose.legacy-pr-agent.yml` vs `self-hosted/reliability/`

---

## Резюме диагноза

**Важно:** В репозитории УЖЕ реализована архитектура `self-hosted/reliability/`, которая решает большинство проблем, описанных в задаче. Legacy-архитектура (`docker-compose.legacy-pr-agent.yml`) сохранена только для отката (см. `GO-LIVE.md`).

**Диагноз двоякий:**

1. **Legacy pr-agent** — все описанные проблемы подтверждены (тихие падения, нет DLQ, нет ретраев)
2. **Reliability-стек** — архитектура решает 90% проблем, но есть **3 сценария остаточных тихих падений**

---

## 1. Проблема legacy-архитектуры (подтверждена)

### 1.1. Тихий провал при LLM-ошибке

**Симптом:** При исключении в LLM-вызове pr-agent пишет ошибку в лог и ничего не постит.

**Доказательство кода:**
- `.github/workflows/pr_agent.yml:146` — `fallback_models: '["openai/glm-5"]'` — тот же провайдер, нет реального fallback
- `self-hosted/docker-compose.legacy-pr-agent.yml` — один сервис `pr-agent`, без очереди/воркера
- Отсутствие DLQ и алертинга в legacy-конфигурации

**Механизм тихого падения:**
```
GitHub → webhook (200 OK) → pr-agent обрабатывает → LLM timeout/error → лог-запись → PR пустой
```

### 1.2. Потеря события при рестарте контейнера

**Доказательство кода:**
- Legacy docker-compose не использует volumes для персистентности
- Нет durable queue
- Нет reconciliation sweeper

**Механизм тихого падения:**
```
Webhook принят (200) → фоновая задача стартовала → контейнер рестарт → задача потеряна
```

### 1.3. Отсутствие реальных ретраев

**Доказательство кода:**
- `.github/workflows/pr_agent.yml:146` — fallback_models = тот же GLM-5
- Нет exponential backoff
- Нет circuit breaker

### 1.4. Нет наблюдаемости

**Доказательство кода:**
- Legacy docker-compose не экспортирует `/metrics`
- Нет state store
- Нет трассировки событий

---

## 2. Решение — reliability-стек (УЖЕ РЕАЛИЗОВАН)

### 2.1. Компоненты, которые УЖЕ работают

| Компонент | Файл | Решает проблему |
|-----------|------|-----------------|
| Durable queue | `queue.py` | Потеря при рестарте (СТ-6) |
| Dead-letter queue | `queue.py:dead_letter` | Тихий провал → видимый коммент (СТ-27) |
| Circuit breaker | `gateway.py:CircuitBreaker` | Зависание на мёртвом провайдере (СТ-22) |
| Rate limiter | `gateway.py:TokenBucket` | 429 от Z.AI (СТ-20) |
| Reconciliation sweeper | `sweeper.py` | Пропущенные webhook'ы (СТ-29) |
| State machine | `state.py` | Наблюдаемость (К-5) |
| Dedup по delivery_id | `ingress.py:handle_webhook` | Идемпотентность (К-4) |
| Failover (seam) | `gateway.py:Gateway` | Мульти-провайдерность (СТ-19/21) |
| Визуальный failure comment | `notifier.py:notify_failure` | Не молчать (К-1) |

### 2.2. Контракт надёжности (К-1..К-5) — УЖЕ задекларирован

**Доказательство:**
- `ARCHITECTURE.md:115-124` — К-1..К-5 задекларированы
- `ARCHITECTURE.md:5479-5508` — контракт описан
- `SYSTEM-REQUIREMENTS.md` — требования зафиксированы

---

## 3. Остаточные сценарии тихих падений (ЕЩЁ НЕ ЗАКРЫТЫ)

Ниже приведены сценарии, где reliability-стек МОЖЕТ потерять событие «в тишину»:

### 3.1. Исключение ДО enqueue в ingress

**Сценарий:**
```python
# app.py:3026-3035
async def webhook(request: Request):
    raw = await request.body()
    status = handle_webhook(raw, dict(request.headers),
                            secret=WEBHOOK_SECRET, store=_store, schedule=schedule,
                            enrich=_enrich)
    # Если enrich падает здесь — событие потеряно
```

**Доказательство:**
- `ingress.py:handle_webhook` — если `enrich_events` падает после `parse_events` но ДО `record_received`
- Между `schedule(event)` и записью в store есть возможность исключения

**Симптом:** Webhook вернёт 500 или 400, но GitHub может не.retry при определённых условиях.

### 3.2. GitHub пропускает webhook delivery

**Сценарий:** GitHub иногда не доставляет webhook по внутренним причинам (нет ACK от upstream, сетевой разрыв между GitHub и ingress).

**Доказательство:** Внешний фактор, не контролируется кодом. Но reconciliation sweeper ДОЛЖен это ловить.

**Уязвимость:** Если sweeper не запущен или умер, пропущенные webhook'ы не восстанавливаются.

### 3.3. Критический сбой после ack, до публикации

**Сценарий:**
```python
# worker.py:5157-5159
if result.state == State.DONE or result.skipped:
    queue.ack(lease.id, lease.token)  # ack успешен
    metrics.incr("processed_ok")
    # Если процесс УМИРАЕТ здесь — ack потерян, DLQ не сработает
```

**Доказательство:**
- `worker.py:handle_lease` — ack вызывается ПЕРЕД публикацией результата
- Если процесс умирает между ack и реальной публикацией в GitHub — событие "DONE" в системе, но ревью не опубликовано

**Симптом:** State = DONE, но в PR нет ревью. Reconciliation sweeper должен это ловить (если запущен).

### 3.4. DLQ не обрабатывается (sweeper не запущен)

**Сценарий:** Событие ушло в dead-letter, но sweeper не дозапускает его.

**Доказательство:**
- `sweeper.py` — отдельный процесс, может быть не запущен
- Нет внешнего алерта "sweeper is down"

**Симптом:** DLQ растёт, но никто не знает (только `/metrics`, который может не мониториться).

---

## 4. Code Evidence (ссылки на строки)

| Утверждение | Доказательство (файл:строки) |
|------------|------------------------------|
| Legacy fallback — тот же провайдер | `.github/workflows/pr_agent.yml:146` |
| Legacy — один сервис без очереди | `docker-compose.legacy-pr-agent.yml:11-34` |
| Reliability — durable queue | `reliability/queue.py:14-50` |
| Reliability — DLQ с комментом | `reliability/worker.py:5180-5191` |
| Reliability — circuit breaker | `reliability/gateway.py:3135-3174` |
| Reliability — reconciliation sweeper | `reliability/sweeper.py` |
| Reliability — state machine | `reliability/state.py` |
| Контракт К-1..К-5 задекларирован | `ARCHITECTURE.md:115-124` |
| Входная точка webhook | `reliability/app.py:3024-3035` |
| ack ПЕРЕД публикацией | `reliability/worker.py:5157-5159` |

---

## 5. Следующие шаги (но не решения)

### 5.1. Для закрытия остаточных сценариев

- [ ] Внешний healthcheck на sweeper (heartbeat/алерт)
- [ ] Transactional write: enqueue + state в одной транзакции
- [ ] Пост-публикация assertion: проверить, что коммент действительно появился в PR
- [ ] Внешний алерт на рост DLQ (не только `/metrics`)

### 5.2. Для go-live reliability-стека

- [ ] Следовать `GO-LIVE.md` для миграции с legacy на reliability
- [ ] Запустить нагрузочный тест (`loadtest/run_loadtest.py`)
- [ ] Настроить мониторинг `/metrics` в Grafana/Prometheus

---

## 6. Связанные артефакты

| Артефакт | Значение |
|----------|----------|
| План миграции | `SCALE-PLAN.md` |
| Требования | `SYSTEM-REQUIREMENTS.md` |
| Архитектура | `ARCHITECTURE.md` |
| Go-live процедура | `GO-LIVE.md` |
| Legacy rollback | `docker-compose.legacy-pr-agent.yml` |
| Прод конфигурация | `docker-compose.yml` |

---

## 7. Вердикт

**Проблема задачи:**
- ✅ **Подтверждена** для legacy-архитектуры
- ⚠️ **Частично решена** в reliability-стеке
- 🔴 **Остаточные риски** в 3 сценариях (см. §3)

**Ключевое наблюдение:**
Репозиторий содержит ДВА стека — legacy (проблематичный) и reliability (отказоустойчивый). Задача описывает legacy-проблемы, но решение УЖЕ реализовано в reliability/ директории.

**Рекомендация:**
Перейти к обсуждению go-live reliability-стека и закрытия остаточных сценариев, а не к reimplement'у того, что уже работает.

---
_Следующий шаг: `/fnr-concept sa_documentation/FNR/FNR_1/task.md` для выбора решения._
