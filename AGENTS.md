# AGENTS.md — runbook для AI-агента: установка и настройка PR-Agent

Ты — AI-агент (Claude Code, Cursor, Copilot и т.п.). Задача: подключить авто-CodeReview
(**PR-Agent** + OpenAI-совместимая модель, в шаблоне **GLM-5 через Z.AI**) к репозиторию
пользователя и настроить инфраструктуру так, чтобы всё заработало. Действуй строго по
этому документу. Не выдумывай значения — если чего-то не хватает, спрашивай пользователя.

## Что это

[PR-Agent](https://github.com/qodo-ai/pr-agent) (Qodo.ai) добавляет к каждому Pull Request
**автоописание**, **code review** и **предложения по коду**. Модель подключается через
litellm (любой OpenAI-совместимый endpoint). Работает в двух режимах развёртывания — сначала
выбери режим, потом иди по его чеклисту.

## Карта репозитория (что где лежит)

| Файл | Назначение | Куда попадает |
|------|-----------|---------------|
| `.github/workflows/pr_agent.yml` | Workflow для режима A (Actions) | в `.github/workflows/` **целевого** репо |
| `self-hosted/register-app.html` | Браузерная форма создания GitHub App (manifest flow) | открывается в браузере локально |
| `self-hosted/finish-registration.sh` | Обмен `code` → `dokploy.env` (App ID, secret, ключ) | запускается локально |
| `self-hosted/docker-compose.yml` | Контейнер webhook-приложения (режим B) | деплоится на хост (Dokploy) |
| `self-hosted/.pr_agent.toml` | Конфиг поведения ревью (язык, фокус, авто-тулы) | в **корень целевого** репо |
| `self-hosted/SETUP.md` | Полный human-runbook режима B + таблица из 9 граблей | справочник |
| `README.md` | Обзор + цикл «правка → повторная проверка» после ревью | справочник |

## Шаг 0. Выбери режим

Задай пользователю (или определи по фактам) один вопрос — **доступны ли GitHub Actions в целевом репо?**

| Признак | Режим | Куда идти |
|--------|-------|----------|
| Actions работают (публичный репо или есть минуты), нужен простейший путь | **A. GitHub Actions** | [Режим A](#режим-a--github-actions) |
| Actions **недоступны**: аккаунт залочен по биллингу (`account is locked due to a billing issue`), кончились минуты, запрет политикой; или нужен диалог `/ask` и авто-ревью на push из коробки | **B. Self-hosted webhook App** | [Режим B](#режим-b--self-hosted-webhook-app) |

> ⚠️ Self-hosted **runner Actions биллинг-лок НЕ обходит** — лок выключает саму оркестрацию Actions, job'ы не диспатчатся. Только webhook App (режим B) реально работает мимо Actions.

---

## Режим A — GitHub Actions

Один файл `.github/workflows/pr_agent.yml` — всё делается в целевом репо. Этот режим агент может пройти автономно (есть доступ к репо и правам на секреты).

1. **Скопируй workflow.** Помести `pr_agent.yml` в `.github/workflows/` целевого репозитория. Путь обязателен — GitHub ищет workflow только там.

2. **Определи провайдера и модель.** Спроси пользователя (или посмотри, что уже есть). Нужно 4 значения:
   - `OPENAI_API_BASE` — endpoint провайдера (шаблон `https://api.z.ai/api/coding/paas/v4`).
   - `config.model` и `config.model_turbo` — id модели в формате litellm, обычно `openai/<модель>` (шаблон `openai/glm-5`).
   - `config.fallback_models` — JSON-массив строкой, например `'["openai/glm-5"]'`.
   - `config.custom_model_max_tokens` — лимит контекста модели (шаблон `128000`).
   Обнови все три job'а (`ai-describe`, `ai-review`, `ai-suggest`) одинаково.

3. **Настрой секрет.** Определись с именем ключа (шаблон: `ZAI_API_KEY`). В workflow: `OPENAI_KEY: ${{ secrets.<ИМЯ> }}`.
   - Через web-интерфейс: **Settings → Secrets and variables → Actions**.
   - Или из CLI (не печатай сам ключ в историю, читай из переменной окружения):
     ```bash
     gh secret set ZAI_API_KEY --repo <owner>/<repo> --body "$ZAI_API_KEY"
     ```
   - Никогда не вписывай сырой ключ в yml. Только `${{ secrets.* }}`.

4. **Разберись с `environment: production`.** Шаблон привязывает каждый job к GitHub Environment `production`.
   - Оставляешь — создай окружение `production` и положи секрет в него (`--env production` у `gh secret set`).
   - Не нужно — удали строку `environment: production` из всех трёх job'ов, тогда возьмётся repository secret.

5. **Проверь целевую ветку.** По умолчанию триггер на PR в любую ветку. Нужен только `main` — добавь `branches: [main]` под `types` в блоке `on.pull_request`.

6. **Убери лишние функции (опционально).** Три job'а независимы. Не нужны предложения по коду — удали job `ai-suggest`. Не нужно описание — удали `ai-describe`.

7. **Проверь, что Actions включены.** **Settings → Actions → General** — Actions разрешены, workflow permissions позволяют запись (конфиг сам запрашивает `contents/issues/pull-requests: write`).

8. **Проверь работу.** Открой тестовый PR (или переоткрой существующий) в целевую ветку. Три job'а должны появиться в **Actions** и отписать комментарии. Падает — проверь: имя секрета совпадает, endpoint отвечает, модель существует у провайдера, окружение `production` есть (если оставлено).

> ⚠️ В режиме A триггера на `push`/`synchronize` нет (только `opened/reopened/ready_for_review` + `issue_comment`). После пуша правок повторное ревью запускается **вручную** — коммент `/review` (и `/improve`, если правил предложения). Подробнее — раздел «После первого ревью» в [`README.md`](README.md).

### Матрица значений под шаблон (режим A)

| Что меняем | Где в yml | Шаблон |
|-----------|-----------|--------|
| Endpoint | `OPENAI_API_BASE` | `https://api.z.ai/api/coding/paas/v4` |
| Модель | `config.model`, `config.model_turbo` | `openai/glm-5` |
| Fallback | `config.fallback_models` | `'["openai/glm-5"]'` |
| Контекст | `config.custom_model_max_tokens` | `128000` |
| Имя ключа | `OPENAI_KEY: ${{ secrets.X }}` | `ZAI_API_KEY` |
| Окружение | `environment:` | `production` |
| Функции | `github_action_config.auto_*` | describe / review / improve |

---

## Режим B — Self-hosted webhook App

PR-Agent крутится как **webhook GitHub App** на сервере пользователя, мимо Actions. Полный человекочитаемый runbook со всеми граблями — [`self-hosted/SETUP.md`](self-hosted/SETUP.md); ниже — агентский порядок действий.

### Что агент может сам, а что требует человека

| Шаг | Кто выполняет |
|-----|---------------|
| Подготовка файлов, формы, env, `.pr_agent.toml`, деплой-манифеста | 🤖 агент |
| **Создание GitHub App** (клик «Create» в браузере) | 👤 **только человек** — GitHub блокирует создание App через API/токен (grabля #2). Агент готовит форму и точную инструкцию. |
| Копирование `code` из адресной строки после редиректа | 👤 человек (агент говорит, что скопировать) |
| `finish-registration.sh <code>`, правка `dokploy.env`, деплой на Dokploy, установка App на репо | 🤖 агент (если есть доступ к машине/хосту) либо 👤 по инструкции агента |

Будь честен с пользователем: шаг создания App интерактивный. Всё остальное автоматизируемо.

### Prereqs (проверь до старта)

- **Хост под Docker Compose** — Dokploy-инстанс (или обычный Docker/VPS, Coolify, k8s — схема та же, меняется способ прокинуть env и порт `3000`).
- **Хостнейм**, резолвящийся на этот хост (для webhook).
- **Ключ Z.AI GLM-5** (или другого OpenAI-совместимого провайдера).
- Локально: `curl`, `python3`, `base64` (нужны `finish-registration.sh`).

### B1. Создать GitHub App (браузер, manifest flow)

Открой [`self-hosted/register-app.html`](self-hosted/register-app.html) в браузере. Заполни два поля:
- **Your repo** — `owner/name` целевого репо.
- **Webhook base URL** — где будет крутиться App, **без слеша в конце** (напр. `http://pr-agent.<ip>.traefik.me` или свой домен).

Нажми **Create GitHub App →**, затем на странице GitHub — **Create**. App сам сгенерит App ID, приватный ключ и webhook secret. Манифест уже проставляет минимальные права (`pull_requests:write`, `issues:write`, `contents:read`, `metadata:read`) и события `pull_request, issue_comment, push` (push нужен для авто-ре-ревью).

### B2. Забрать креды

GitHub редиректит на `<base>/?code=…` (страница может не загрузиться — это норма). Скопируй `code` из адресной строки и локально:
```bash
cd self-hosted
bash finish-registration.sh <code>      # создаст dokploy.env
```
Скрипт запишет `GITHUB_APP_ID`, `GITHUB_WEBHOOK_SECRET`, `GITHUB_PRIVATE_KEY_B64` в `dokploy.env` и напечатает slug + ссылку на установку. Приватный ключ **никуда не уходит** — только в локальный файл.
> ⚠️ `code` одноразовый и живёт ~1 час. Просрочил — повтори B1.

Впиши в `dokploy.env` свой `OPENAI_KEY` (заглушку `REPLACE_WITH_YOUR_ZAI_GLM5_KEY` замени на реальный ключ).

### B3. Задеплоить контейнер

На Dokploy: Create Service → **Compose**:
- Provider: Git → **этот** репозиторий, path `self-hosted/docker-compose.yml`.
- **Environment**: вставь переменные из `dokploy.env` (см. таблицу ниже).
- **Domains**: Host = твой webhook-хост, **Container Port `3000`**.
- **Deploy**.

Образ собирается из upstream (`qodo-ai/pr-agent`, target `github_app`) — локальный клон не нужен. Entrypoint на старте сам пишет `.secrets.toml` из env (см. грабли #3–#7).

#### Env-переменные (режим B)

| Переменная | Обяз. | Дефолт | Назначение |
|-----------|:----:|--------|-----------|
| `OPENAI_KEY` | ✅ | — | ключ провайдера модели |
| `GITHUB_APP_ID` | ✅ | — | из `finish-registration.sh` |
| `GITHUB_WEBHOOK_SECRET` | ✅ | — | из `finish-registration.sh` |
| `GITHUB_PRIVATE_KEY_B64` | ✅ | — | base64 приватного ключа, одной строкой |
| `OPENAI_API_BASE` | | `https://api.z.ai/api/coding/paas/v4` | endpoint провайдера |
| `CONFIG_MODEL` | | `openai/glm-5` | id модели (litellm) |
| `CONFIG_MAX_TOKENS` | | `128000` | лимит контекста |

> Сменить провайдера/модель = задать `OPENAI_API_BASE`, `CONFIG_MODEL`, `CONFIG_MAX_TOKENS` host-side (в этих env). **В `.pr_agent.toml` репо это НЕ переопределяется** — `[config]`/`[openai]` host-restricted (грабля #5).

### B4. Установить App на репозиторий

Ссылку печатает `finish-registration.sh`: `https://github.com/apps/<slug>/installations/new` → выбери целевой репо.

### B5. Положить `.pr_agent.toml` в корень целевого репо

Скопируй [`self-hosted/.pr_agent.toml`](self-hosted/.pr_agent.toml) в **корень целевого** репо (не этого). Он задаёт: какие тулы авто-запускаются на открытие PR (`pr_commands`), ре-ревью на push (`handle_push_trigger` + `push_commands`), язык и фокус ревью (`extra_instructions`). Правки применяются **вживую, без редеплоя** App.

### B6. Проверить работу

Открой тестовый PR → `describe/review/improve` появятся в течение ~минуты. Коммент `/review` перезапускает ревью; `/ask <вопрос>` — диалог; `/improve` — предложения. Цикл «правка → повторная проверка» описан в [`README.md`](README.md).

### Грабли режима B (частые причины «не работает»)

Полная таблица из 9 пунктов — в [`SETUP.md`](self-hosted/SETUP.md). Самые критичные для доставки webhook:

| Симптом | Причина | Что делать |
|---------|---------|-----------|
| webhook `500 tls: certificate is valid for ...traefik.default` / `308` redirect | **traefik.me — только HTTP**, TLS self-signed | Webhook URL App = `http://`; в домене Dokploy **выключи HTTPS и force-redirect**; **редеплой**, чтобы Traefik пересобрал роутер |
| `403/401` на доставке webhook | webhook secret не совпал | сверь `GITHUB_WEBHOOK_SECRET` с тем, что в App |
| `200`, но комментария нет | App принял, упал внутри | смотри логи контейнера |
| `has no attribute 'app_id'` / неправильная модель | dotted env vars (`OPENAI.KEY`, `CONFIG.MODEL`) игнорируются | уже решено: entrypoint пишет `.secrets.toml` — проверь, что деплой свежий |
| `OpenAIException - Country ... not supported` | модель ушла на `api.openai.com`, регион-блок | `OPENAI_API_BASE`/`CONFIG_MODEL` не применились → редеплой (стал образ) |

### Самодиагностика (агент делает сам)

1. **GitHub App → Advanced → Recent Deliveries** — статус последней доставки: `500 tls:` → грабля traefik; `403/401` → секрет; `200` без комментария → логи контейнера.
2. **Логи контейнера** на старте: НЕ должно быть `Settings file not found: /app/pr_agent/settings/.secrets.toml` (безобидный `settings_prod` — ок).
3. **Проверка auth без сервера**: с App ID + `.pem` (`echo "$GITHUB_PRIVATE_KEY_B64" | base64 -d > app.pem`) можно сминтить App-JWT и дёрнуть `GET /app`, `GET /app/installations`, `GET /app/hook/deliveries` — увидишь, что именно прислал GitHub и что вернул сервер.

---

## Красные флаги (СТОП — обоих режимов)

- Ключ провайдера, webhook secret или приватный ключ **в открытом виде** в yml/коммите/логе. В режиме A — только `${{ secrets.* }}`; в режиме B — только через env хоста и локальный `dokploy.env` (не коммить его).
- Печать сырого ключа/секрета в лог или вывод команды.
- `permissions`/права шире, чем нужно. Режим A: достаточно `contents/issues/pull-requests: write`. Режим B (App-манифест): `pull_requests:write`, `issues:write`, `contents:read`, `metadata:read`.
- Webhook URL на `https://` для `*.traefik.me` — доставка сломается (см. грабли).

## Справка

- Все параметры конфига PR-Agent: https://qodo-merge-docs.qodo.ai/usage-guide/configuration_options/
- Список провайдеров litellm: https://docs.litellm.ai/docs/providers
- Полный runbook self-hosted + грабли: [`self-hosted/SETUP.md`](self-hosted/SETUP.md)
- Цикл «правка → повторная проверка» после ревью: [`README.md`](README.md)
