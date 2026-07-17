# PR-Agent + GLM-5 → авто CodeReview на GitHub

Готовый GitHub Actions конфиг, который добавляет к вашим Pull Request:

- 📝 **автоописание** PR (~1 мин)
- 🔍 **code review** (~5 мин)
- 💡 **предложения по улучшению** кода (может быть долго)

Работает на [PR-Agent](https://github.com/qodo-ai/pr-agent) — open-source инструмент от [Qodo.ai](https://www.qodo.ai/). Под капотом [litellm](https://github.com/BerriAI/litellm), поэтому подключается **любая OpenAI-совместимая модель**. В этом конфиге используется **GLM-5** через [Z.AI](https://z.ai/).

---

## Два режима развёртывания

| Режим | Когда | Гайд |
|-------|-------|------|
| **A. GitHub Actions** | Actions доступны (публичный репо или есть минуты). Проще всего. | [Быстрый старт](#быстрый-старт-3-шага) ниже |
| **B. Self-hosted (Dokploy)** | Actions **недоступны**: аккаунт залочен по биллингу (`account is locked due to a billing issue`), кончились минуты, запрет политикой. Свой сервер, webhook GitHub App — мимо Actions. | [`self-hosted/DEPLOY.md`](self-hosted/DEPLOY.md) |

> Режим B — то же ревью как **отказоустойчивое webhook-приложение** на вашей инфре
> (не молчит при сбоях: любой провал → видимый коммент в PR). Сквозная инструкция от
> нуля до работы под нагрузкой — [`self-hosted/DEPLOY.md`](self-hosted/DEPLOY.md);
> как устроено и как чинить — [`self-hosted/ARCHITECTURE.md`](self-hosted/ARCHITECTURE.md).
> Важно: self-hosted **runner Actions биллинг-лок НЕ обходит** — только webhook App.

---

## Быстрый старт (3 шага)

> Это режим **A** (GitHub Actions). Для self-hosted → [`self-hosted/DEPLOY.md`](self-hosted/DEPLOY.md).

### 1. Положите конфиг в репозиторий

Скопируйте [`.github/workflows/pr_agent.yml`](.github/workflows/pr_agent.yml) в тот же путь вашего репозитория:

```
ваш-репозиторий/
└── .github/
    └── workflows/
        └── pr_agent.yml
```

GitHub Actions подхватит workflow автоматически, ничего запускать вручную не нужно.

### 2. Добавьте API-ключ в секреты

Получите ключ на [Z.AI](https://z.ai/) и добавьте его в репозиторий:

**Settings → Secrets and variables → Actions → New repository secret**

- **Name:** `ZAI_API_KEY`
- **Secret:** ваш ключ Z.AI

> Конфиг привязан к GitHub Environment `production` (`environment: production` в каждом job). Либо создайте окружение с таким именем и положите секрет туда, либо уберите строки `environment: production` — тогда возьмётся repository secret. Подробнее — см. [Настройка](#настройка).

`GITHUB_TOKEN` добавлять не нужно — GitHub Actions выдаёт его автоматически.

### 3. Откройте Pull Request в `main`

Откройте PR (или переоткройте / снимите статус draft) — три job'а запустятся сами и отпишутся комментариями прямо в PR.

---

## Что делает конфиг

Три независимых job'а, каждый — отдельный вызов PR-Agent:

| Job | Команда PR-Agent | Что делает |
|-----|------------------|------------|
| `ai-describe` | `auto_describe` | Генерирует описание PR |
| `ai-review`   | `auto_review`   | Проводит code review |
| `ai-suggest`  | `auto_improve`  | Предлагает улучшения кода |

Все три триггерятся на:

- `pull_request`: `opened`, `reopened`, `ready_for_review`
- `issue_comment` — комментарии-команды в PR (например `/review`, `/describe`, `/improve`)

Фильтр `if: github.event.sender.type != 'Bot'` не даёт агенту реагировать на собственные комментарии (защита от петли).

---

## Настройка

Все параметры задаются через `env` в каждом job — это переопределения [конфига PR-Agent](https://qodo-merge-docs.qodo.ai/usage-guide/configuration_options/).

### Сменить модель

Замените GLM-5 на любую OpenAI-совместимую модель — поменяйте три поля и endpoint:

```yaml
OPENAI_API_BASE: "https://api.z.ai/api/coding/paas/v4"   # endpoint провайдера
config.model: "openai/glm-5"                              # основная модель
config.model_turbo: "openai/glm-5"                        # быстрая модель
config.fallback_models: '["openai/glm-5"]'               # запасные
config.custom_model_max_tokens: "128000"                 # лимит контекста модели
```

Имя ключа-секрета тоже можно переименовать (`ZAI_API_KEY` → своё), поправив `OPENAI_KEY: ${{ secrets.ВАШ_КЛЮЧ }}`.

### Включить / выключить отдельные функции

В каждом job ровно один флаг стоит в `true`:

```yaml
github_action_config.auto_describe: "true"
github_action_config.auto_review: "false"
github_action_config.auto_improve: "false"
```

Не нужен, например, `auto_improve` (он самый долгий и «шумный») — удалите job `ai-suggest` целиком.

### Environment `production`

Каждый job использует `environment: production`. Это даёт слой контроля (approvers, отдельные секреты), но требует, чтобы окружение с таким именем существовало и содержало `ZAI_API_KEY`. Если не нужно — удалите строку `environment: production` из всех трёх job'ов и положите секрет как обычный repository secret.

### Другая целевая ветка

Триггер `pull_request` срабатывает на PR в **любую** ветку. Нужен только `main` — добавьте фильтр:

```yaml
on:
  pull_request:
    types: [opened, reopened, ready_for_review]
    branches: [main]
```

---

## Требования

- Публичный или приватный GitHub-репозиторий с включёнными Actions
- API-ключ [Z.AI](https://z.ai/) (или другого OpenAI-совместимого провайдера)
- Права workflow: `issues: write`, `pull-requests: write`, `contents: write` — уже прописаны в конфиге

---

## Стоимость и приватность

- **Оплата** — на стороне провайдера модели (Z.AI). PR-Agent и GitHub Actions бесплатны для публичных репозиториев (для приватных действуют лимиты минут Actions).
- **Приватность** — diff вашего PR уходит провайдеру модели. Для корп-кода проверьте политику провайдера и условия обработки данных.

---

## Ссылки

- PR-Agent: https://github.com/qodo-ai/pr-agent
- Документация Qodo Merge: https://qodo-merge-docs.qodo.ai/
- litellm (список провайдеров): https://docs.litellm.ai/docs/providers
- Z.AI: https://z.ai/

---

*Конфиг предоставлен как есть. Ключи в файле не хранятся — только ссылки на GitHub Secrets.*
