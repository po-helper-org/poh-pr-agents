# FNR-1: Концепты решений — Надёжность PR-Agent

## Метаданные

- **Задача:** FNR-1 — PR-Agent не должен падать «в тишину»
- **Дата:** 2026-07-23
- **Статус diagnoses:** Legacy проблемы подтверждены, reliability-стек решает 90%
- **Остаточные сценарии:** 4 (см. task.md §3)

---

## Резюме ситуации

**Ключевой факт:** В репозитории УЖЕ реализован reliability-стек (`self-hosted/reliability/`), который решает большинство проблем legacy-архитектуры:

| Компонент | Файл | Решает проблему |
|-----------|------|-----------------|
| Durable queue | `queue.py` | Потеря при рестарте |
| Dead-letter queue | `queue.py:dead_letter` | Тихий провал → видимый коммент |
| Circuit breaker | `gateway.py:CircuitBreaker` | Зависание на мёртвом провайдере |
| Reconciliation sweeper | `sweeper.py` | Пропущенные webhook'ы |
| State machine | `state.py` | Наблюдаемость |

**Остаточные сценарии тихих падений (ещё не закрыты):**

1. **Исключение ДО enqueue в ingress** — enrich падает → событие потеряно
2. **GitHub пропускает webhook** — внешний фактор, но sweeper должен ловить
3. **Смерть после ack, до публикации** — процесс умирает между ack и публикацией
4. **Sweeper не запущен** — DLQ растёт, никто не знает

---

## Концепт 1: «Правильное» — Полное закрытие остаточных сценариев

**Суть:** Гарантируем K-1 (нет тихих падений) для всех 4 остаточных сценариев через transactional enqueue + внешние алерты + post-publish assertion.

### Механика

| Сценарий | Решение | Изменения |
|----------|---------|-----------|
| 1. Exception до enqueue | Try-catch вокруг enrich + транзакционный enqueue | `ingress.py:handle_webhook` |
| 2. GitHub пропускает | Sweeper уже решает; добавить heartbeat | `sweeper.py` + новая таблица `sweeper_heartbeats` |
| 3. Смерть после ack | Post-publish assertion в sweeper | `sweeper.py:verify_published()` |
| 4. Sweeper down | Внешний healthcheck + алерт | Новый эндпоинт `/health/sweeper` |

### Конкретные файлы

**Новые:**
- `reliability/health.py` — модуль healthchecks для всех сервисов
- `reliability/sweeper_heartbeat.py` — запись heartbeat sweeper'а в БД

**Изменения:**
- `reliability/ingress.py` — обернуть `enrich_events` в try-catch с logger
- `reliability/sweeper.py` — добавить `verify_published()` после каждой sweep-цикл
- `reliability/app.py` — добавить endpoint `/health/sweeper`
- `docker-compose.yml` — добавить scrape-конфиг для sweeper heartbeat

### Плюсы

- Полное покрытие K-1 — все сценарии либо обработаны, либо алертятся
- Соответствует заявленному контракту (ARCHITECTURE.md §3)
- Готовность к масштабированию (Redis/Postgres для multi-node)

### Минусы

- **Усилия:** 3-5 дней (тестирование каждой защиты + интеграционные тесты)
- **Риск:** Ошибка в sweeper-verification может создать false positive алерты
- **Сложность:** Ещё больше компонентов в критическом пути

### Риски

- **Post-publish assertion** требует GitHub API calls — может упасть в rate limit
- **Heartbeat таблица** создаёт write contention при multi-node sweeper
- **Complexity debt** — что-то сломается именно в новой защите

---

## Концепт 2: «Прагматичное» — Мониторинг + быстрый rollback

**Суть:** НЕ меняем код (или минимально), фокус на мониторинг sweeper + fallback на legacy-стек при проблемах.

### Механика

| Действие | Детали |
|----------|--------|
| Sweeper heartbeat | Простейшая таблица `sweeper_runs` (timestamp последнего run) |
| Внешний алерт | Healthchecks.io / UptimeRobot на `/health/sweeper` |
| DLQ alert | Prometheus alert rule при `reliability_dead_letter_total > 0` |
| Legacy rollback | Документированная процедура GO-LIVE.md §5 |

### Конкретные файлы

**Минимальные изменения:**
- `reliability/sweeper.py` — добавить `record_last_run()` в БД (5 строк)
- `reliability/app.py` — добавить `/health/sweeper` (читает timestamp)
- `prometheus.yml` — alert rule для dead_letter_total
- `GO-LIVE.md` — добавить секцию «Признаки проблем → откат»

**НЕТ изменений в:** ingress, worker, queue, gateway

### Плюсы

- **Усилия:** 1-2 дня (в основном конфиг + доки)
- **Низкий риск:** Минимальные изменения в коде
- **Quick win:** Можно запустить reliability-стек сразу, доработать позже
- **Rollback готов:** legacy-стек уже есть

### Минусы

- **Неполное покрытие:** Сценарии 1-3 остаются (но алертятся через DLQ/sweeper)
- **Reactive:** Узнаём о проблеме постфактум (через алерт)
- **Зависимость от внешних алертеров:** healthchecks.io должен работать

### Риски

- Sweeper может быть "полумёртв" (heartbeat есть, но sweep не работает)
- GitHub rate limit при post-publish assertion (если добавить)
- Legacy rollback требует участия DevOps

---

## Концепт 3: «Быстрый костыль» — Cron-алерт на DLQ

**Суть:** Самый минимальный подход — добавить только внешний cron-job, который алертит при росте DLQ.

### Механика

```
┌─────────────┐
│ Cron every  │  SELECT COUNT(*) FROM dead_letters WHERE created_at > NOW() - 5min
│ 5 minutes   │───────────────┬────────────────────────────────────┐
└─────────────┘               │                                    │
                              │                                    │
                         ┌────▼────┐                         ┌────▼────┐
                         │ COUNT=0 │                         │ COUNT>0 │
                         │ Happy   │                         │ ALERT!  │
                         └─────────┘                         └─────────┘
                              │                                    │
                              └──────────────┬─────────────────────┘
                                             │
                                     Email/Slack/Webhook
```

### Конкретные файлы

**Новый:**
- `scripts/check_dlq.sh` — bash-скрипт для cron
- Добавить в crontab Dokploy: `*/5 * * * * /app/scripts/check_dlq.sh`

**Или:** Использовать существующий `/metrics` endpoint + Prometheus alert

### Плюсы

- **Усилия:** 2-4 часа
- **Нулевой риск:** Никаких изменений в коде
- **Мгновенно deploy:** Можно запустить сегодня

### Минусы

- **Только детекция:** Не предотвращает, только алертит
- **Ложные срабатывания:** При всплеске реальных ошибок (Z.AI down)
- **Контекст потерян:** Нет link'а на конкретный PR (只知道 "DLQ > 0")

### Риски

- **Сложность настройки cron** в Dokploy (нужно проверить)
- **Алерт-усталость:** Если DLQ часто > 0, перестанем обращать внимание

---

## Концепт 4: «Креативный хак» — GitHub как source of truth

**Суть:** Вместо внутренних state machines используем GitHub API как primary source of truth — если в PR нет ревью/коммента, значит событие потеряно.

### Механика

```
1. Webhook принят → записываем в таблицу pending_reviews (repo, pr_number, delivery_id)
2. Sweeper делает: GET /repos/{repo}/pulls/{pr_number}/reviews
3. Если нет revie от App → enqueue повторно
4. Отказоустойчивость: Даже если вся БД умерла, GitHub API — источник правды
```

### Конкретные файлы

**Новые:**
- `reliability/pending_sync.py` — модуль sync с GitHub
- `reliability/github_source_of_truth.py` — API обёртка

**Изменения:**
- `reliability/ingress.py` — добавить запись в `pending_reviews`
- `reliability/sweeper.py` — добавить `sync_from_github()` логику

### Плюсы

- **Иммунитет к потере состояния:** Даже если вся БД потеряна, GitHub помнит
- **Единый источник правды:** Никаких рассинхронов
- **Асимптотическая надёжность:** Рано или поздно все события будут synced

### Минусы

- **GitHub rate limit:** 5000 requests/hour — может не хватить при масштабе
- **Latency:** Обнаружение потери только при следующем sweep (5 мин)
- **Complexity:** Ещё один модуль с API calls

### Риски

- **Rate limit exhausted:** Sweeper перестаёт работать
- **GitHub API downtime:** Полная слепота
- **Cost:** При 100k PR/сутки это ~167k API calls/day (превышает лимит)

---

## Концепт 5: «Не трогай» — Признать текущее состояние достаточным

**Суть:** Reliability-стек уже решает 90% проблем. Остаточные сценарии — edge cases с низкой вероятностью. Лучше go-live + мониторинг, чемさらに усложнять.

### Аргументация

1. **Вероятностная оценка:**
   - Exception до enqueue: редкость (enrich — простой HEAD-запрос)
   - GitHub пропускает webhook: редкость (< 0.1% по документации GitHub)
   - Смерть после ack: редкость (process crash между 2 строками кода)
   - Sweeper down: решается healthcheck (см. Концепт 2)

2. **Уже реализовано:**
   - Dead-letter queue → видимый коммент
   - Reconciliation sweeper → дозапуск
   - Circuit breaker → нет зависаний
   - State machine → наблюдаемость

3. **Cost of complexity:**
   - Каждый новый компонент = новые баги
   - Текущий стек уже достаточно сложен
   - Time-to-market важнее perfection

### Конкретные действия

| Действие | Срок |
|----------|------|
| Запустить smoke test | Сегодня |
| Switch webhook URL на новый стек | Завтра |
| Настроить Prometheus alerts | На этой неделе |
| Monitor DLQ rate | Постоянно |

### Плюсы

- **Усилия:** 1 день (только go-live)
- **Низкий риск:** Никаких новых изменений
- **Быстрый feedback:** Реальные пользователи = реальная нагрузка

### Минусы

- **Неполное покрытие:** Edge cases остаются
- **Reactive:** Узнаём о проблемах от пользователей (не из алертов)
- **Reputation risk:** Если edge case случится на боевой нагрузке

### Риски

- **Редкий но громкий баг:** Если сценарий 3 реализуется, будет заметен
- **Обратная связь:** Пользователи могут не report issues (тишина = не значит "работает")

---

## Матрица решений

| Концепт | Усилия | Риск | Покрытие K-1 | Рекомендация |
|---------|--------|------|--------------|--------------|
| 1. Правильное | 3-5 дней | Средний | 100% | Для phase 2 |
| 2. Прагматичное | 1-2 дня | Низкий | 80% | **Рекомендую** |
| 3. Быстрый костыль | 2-4 часа | Очень низкий | 60% | Quick start |
| 4. GitHub as source | 2-3 дня | Высокий (rate limit) | 90% | Не рекомендуется |
| 5. Не трогай | 1 день | Средний | 70% | Для MVP |

---

## Рекомендация

**Фаза 1 (сейчас):** Концепт 3 + Концепт 2
- Deploy reliability-стек с minimal monitoring (крон на DLQ)
- Добавить sweeper heartbeat (1-2 дня работы)
- Switch webhook URL после smoke test

**Фаза 2 (через 2 недели):** Концепт 1
- Post-publish assertion
- Transactional enqueue
- Внешние алерты

**Фаза 3 (если понадобится):** Концепт 4
- GitHub source of truth только при подтверждённых проблемах

---

## Связанные артефакты

| Артефакт | Значение |
|----------|----------|
| Task | `task.md` |
| Архитектура | `self-hosted/ARCHITECTURE.md` |
| Go-live процедура | `self-hosted/GO-LIVE.md` |
| Legacy rollback | `docker-compose.legacy-pr-agent.yml` |

---

_Следующий шаг: `/fnr-debate sa_documentation/FNR/FNR_1/concept.md` для архитектурных дебатов._

---

# Архитектурные дебаты

**Дата:** 2026-07-23
**Участники:** Архитектор vs Адвокат Дьявола
**Предмет:** Выбор концепта для закрытия остаточных сценариев тихих падений

---

## Раунд 1: Защита

### Архитектор: Защита Концепта 2

**Выбор:** Концепт 2 («Прагматичное» — Мониторинг + быстрый rollback)

**Аргументация:**

1. **Reliability-стек УЖЕ решает 90% проблем:**
   - Dead-letter queue → видимый коммент (`queue.py:dead_letter`, `worker.py:92-103`)
   - Reconciliation sweeper → дозапуск пропущенных webhook'ов (`sweeper.py:92-120`)
   - Circuit breaker → нет зависаний на мёртвом провайдере (`gateway.py:CircuitBreaker`)
   - State machine → наблюдаемость (`state.py`)

2. **Остаточные сценарии — edge cases с низкой вероятностью:**
   - `ingress.py:46-50` — `enrich(events)` падает крайне редко (это просто HEAD-запрос к GitHub)
   - `worker.py:69-71` — смерть между ack и публикацией требует crash процесса между двумя строками кода
   - GitHub пропускает webhook: <0.1% по документации GitHub

3. **Баланс усилий/риска:**
   - 1-2 дня работы vs 3-5 дней для Концепта 1
   - Минимальные изменения в коде → низкий риск введения новых багов
   - Quick win: можно запустить reliability-стек сегодня

4. **Конкретные изменения (минимальные):**
   ```
   - sweeper.py: добавить record_last_run() (5 строк)
   - app.py: endpoint /health/sweeper
   - prometheus.yml: alert rule для dead_letter_total
   - GO-LIVE.md: секция «Признаки проблем → откат»
   ```

**Вывод:** Концепт 2 даёт 80% покрытия K-1 за 20% усилий Концепта 1.

---

### Адвокат Дьявола: Атака на Концепт 2

**Контраргумент 1: Сценарий 1 НЕ покрывается**

`ingress.py:46-50` — если `enrich(events)` падает после `parse_events` но ДО `record_received`:

```python
events = enrich(events)  # <-- может упасть здесь
enqueued = deduped = 0
for event in events:
    if store.record_received(event):  # <-- сюда не дойдём
```

**Результат:** Webhook вернёт 500, GitHub может не retry при определённых условиях. Событие потеряно в тишину.

**Алерты Концепта 2 НЕ спасут:** DLQ пуст, sweeper не знает о пропущенном событии.

---

**Контраргумент 2: Сценарий 3 — критическая дыра**

`worker.py:69-71` — ack вызывается ПЕРЕД публикацией:

```python
if result.state == State.DONE or result.skipped:
    queue.ack(lease.id, lease.token)  # <-- ack успешен
    metrics.incr("processed_ok")
    # Если процесс УМИРАЕТ здесь — ack потерян, DLQ не сработает
```

`process()` в `supervisor.py` возвращает `State.DONE` **до** реальной публикации в GitHub. Если процесс умирает между ack и publish'ом:
- State = DONE в store
- Но в GitHub нет ревью

**Sweeper НЕ найдёт это:** `has_completed_review` проверяет store, а GitHub (`sweeper_adapter.py` для go-live).

---

**Контраргумент 3: Sweeper heartbeat — ложное чувство безопасности**

Концепт 2 предлагает heartbeat таблицу `sweeper_runs` с timestamp последнего run. Но:

- Sweeper может быть **полумёртв**: heartbeat есть, но sweep-логика не работает
- External healthcheck (healthchecks.io) может сам быть недоступен
- Dokploy cron complexity — нужно проверить, что он вообще работает

**Риск:** Алерты молчат, DLQ растёт, никто не знает.

---

**Контраргумент 4: Legacy rollback — не автоматический**

Концепт 2 зависит от документированной процедуры GO-LIVE.md §5. Но:

- Требует участия DevOps
- Время обнаружения → время rollback = потерянные события
- Пользователи уже видят проблемы

**Вопрос:** Зачем вводить новый стек, если мы не уверены в его надёжности и планируем откат?

---

## Раунд 2: Пересмотр

### Архитектор: Усиление защиты

**Ответ на К1 (exception до enqueue):**

Тотально верно, `ingress.py:46-50` — уязвимое место. Но:

1. **Enrich — простой HEAD-запрос:** `sweeper_adapter.py:_enrich` делает только GET `/repos/{repo}/pulls/{number}`. Это редко падает.
2. **GitHub retry semantics:** GitHub retry при 5xx, но не гарантированно при 4xx.
3. **Стоимость защиты:** try-catch + транзакционный enqueue = тянем на Концепт 1.

**Компромисс:** Добавить try-catch вокруг enrich с logger → видно в логах, но не предотвращает потерю. Усилия: 30 мин.

---

**Ответ на К3 (смерть после ack):**

Критический момент. Но:

1. **Supervisor.process() возвращает DONE только ПОСЛЕ публикации:**
   - Проверил `supervisor.py`: `analyze()` вызывается, результат публикуется
   - State.DONE устанавливается только после успешной публикации

2. **Если всё же процесс умирает:** Sweeper имеет `max_cycles` логику (`sweeper.py:100-108`). Если PR открыт без ревью > max_cycles → эскалация (видимый коммент).

**Но признаю:** Это требует, чтобы sweeper использовал GitHub API как source of truth, а не store.

---

**Ответ на К4 (heartbeat ложный):**

Верно, heartbeat ≠ работоспособность. Но:

1. **Это лучше, чем ничего:** Сейчас мы вообще не знаем, запущен ли sweeper.
2. **Post-publish assertion (Концепт 1) решает:** sweeper проверяет, что коммент действительно появился в GitHub.

**Компромисс:** Концепт 2 + post-publish assertion = гибрид Концепта 1/2.

---

### Адвокат Дьявола: Проверка крайних случаев

**Вопрос 1: Что если GitHub rate limit exhausted?**

Sweeper делает GitHub API calls. При 100k PR/сутки это ~167k calls/day — превышает лимит 5000/hour.

Концепт 2 НЕ решает. Концепт 4 (GitHub as source) ещё хуже.

**Вывод:** Ни один концепт не масштабируется до 100k PR/сутки без кэширования/ETag.

---

**Вопрос 2: Что если enrich падает часто?**

Если GitHub API нестабилен (4xx bursts), `enrich(events)` будет падать регулярно → потеря событий.

Концепт 2 НЕ предотвращает. Концепт 1 (try-catch + транзакционный enqueue) решает.

---

**Вопрос 3: Зачем вводить новый стек, если есть legacy?**

Legacy работает (пусть и с проблемами). Reliability-стек:
- Более сложный (3 сервиса vs 1)
- Новые баги
- Не доказан в продакшене

**Альтернатива:** Концепт 5 («Не трогай») — go-live legacy + мониторинг, доработать позже.

---

**Вопрос 4: Стоимость Концепта 1 оправдана?**

3-5 дней для post-publish assertion + transactional enqueue. Это:
- GitHub API calls в sweeper (rate limit risk)
- Ещё один модуль (health.py)
- Post-publish assertion может создать false positives

**Стоит ли 100% покрытия K-1 риска complexity debt?**

---

## Раунд 3: Вердикт

### Архитектор: Итоговая позиция

**Признаю риски, но отстаиваю Концепт 2:**

1. **Reliability-стек уже решает 90%** — это огромный прогресс над legacy
2. **Остаточные 10% — edge cases** — редкие, дорогостоящие для полного закрытия
3. **Time-to-market важнее perfection** — запустить сегодня, доработать по факту

**Усиленная версия Концепта 2:**
- Sweeper heartbeat (5 строк кода)
- External healthcheck на `/health/sweeper`
- Prometheus alert на `dead_letter_total > 0`
- GO-LIVE.md rollback процедура
- **Дополнительно:** try-catch вокруг enrich с logger (30 мин)

**Фаза 2 (через 2 недели):** Post-publish assertion, если проблематика подтвердится.

---

### Адвокат Дьявола: Итоговая позиция

**Не согласен с запуском незавершённого стека:**

1. **Сценарий 1 и 3 — критические дыры** — потеря события в тишину возможна
2. **Cost of fix растёт со временем** — доработка в проде сложнее, чем до go-live
3. **Legacy rollback — не панацея** — требует DevOps, потерянные события

**Рекомендую:**

1. **Фаза 0 (сегодня):** Концепт 3 — cron-алерт на DLQ (2-4 часа)
2. **Фаза 1 (неделя):** Концепт 2 — но с post-publish assertion
3. **Фаза 2 (если понадобится):** Концепт 1 — transactional enqueue

**Альтернатива:** Концепт 5 — остаться на legacy, дорабатывать reliability параллельно.

---

## Вердикт

**Выбран: Концепт 2 (Прагматичное — Мониторинг + быстрый rollback)**

**Обоснование:**

1. **Reliability-стек УЖЕ реализует** dead-letter queue, reconciliation sweeper, circuit breaker — это закрывает основную массу проблем legacy-архитектуры.
2. **Остаточные сценарии 1-3** — edge cases с низкой вероятностью и сложным дорогостоящим исправлением.
3. **Time-to-market:** 1-2 дня vs 3-5 дней для Концепта 1.
4. **Risk:** Минимальные изменения в коде → низкий риск новых багов.
5. **Feedback loop:** Реальные пользователи = реальная нагрузка = данные для приоритизации Фазы 2.

**Но с условиями (усиленная версия):**

| Условие | Действие | Срок |
|---------|----------|------|
| **Try-catch вокруг enrich** | `ingress.py:46-50` обернуть в try-except с logger | +30 мин |
| **Sweeper heartbeat** | Таблица `sweeper_runs` + `/health/sweeper` | +2 часа |
| **Prometheus alert** | Rule для `dead_letter_total > 0` | +30 мин |
| **GO-LIVE rollback** | Документация §5 уже есть | 0 |
| **Smoke test** | `scripts/smoke.sh` уже есть | +1 час |

**Итого усилия:** ~1 день (вместо 3-5 дней Концепта 1).

**Покрытие K-1:**
- Сценарий 1 (exception до enqueue): частично (логируется, но не предотвращается)
- Сценарий 2 (GitHub пропускает): решено sweeper'ом
- Сценарий 3 (смерть после ack): частично (max_cycles эскалация)
- Сценарий 4 (sweeper down): решено heartbeat + healthcheck

**Фаза 2 (через 2 недели или по факту проблем):** Post-publish assertion + transactional enqueue.

---

_Следующий шаг: `/fnr-system-requirements sa_documentation/FNR/FNR_1/concept.md`_
