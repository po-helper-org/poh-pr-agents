# AGENTS.md — инструкция для AI-агента по подключению PR-Agent

Ты — AI-агент (Claude Code, Cursor, Copilot и т.п.). Пользователь хочет подключить авто-CodeReview (PR-Agent + OpenAI-совместимая модель) к своему репозиторию. Действуй по этому чеклисту.

## Что это

Один файл `.github/workflows/pr_agent.yml` добавляет к каждому Pull Request: автоописание, code review, предложения по коду. Движок — [PR-Agent](https://github.com/qodo-ai/pr-agent) (Qodo.ai) на GitHub Actions, модель подключается через litellm (любой OpenAI-совместимый endpoint).

## Чеклист подключения

1. **Скопируй workflow.** Помести `pr_agent.yml` в `.github/workflows/` целевого репозитория. Путь обязателен — GitHub ищет workflow только там.

2. **Определи провайдера и модель.** Спроси пользователя (или посмотри, что уже есть). Нужно 4 значения:
   - `OPENAI_API_BASE` — endpoint провайдера (в шаблоне `https://api.z.ai/api/coding/paas/v4`).
   - `config.model` и `config.model_turbo` — id модели в формате litellm, обычно `openai/<модель>` для OpenAI-совместимых (в шаблоне `openai/glm-5`).
   - `config.fallback_models` — JSON-массив строкой, например `'["openai/glm-5"]'`.
   - `config.custom_model_max_tokens` — лимит контекста модели (в шаблоне `128000`).
   Обнови все три job'а (`ai-describe`, `ai-review`, `ai-suggest`) одинаково.

3. **Настрой секрет.** Определись с именем ключа (шаблон: `ZAI_API_KEY`). В workflow: `OPENAI_KEY: ${{ secrets.<ИМЯ> }}`.
   - Сам ключ через web-интерфейс: **Settings → Secrets and variables → Actions**.
   - Или из CLI (не печатай сам ключ в историю, читай из переменной окружения):
     ```bash
     gh secret set ZAI_API_KEY --repo <owner>/<repo> --body "$ZAI_API_KEY"
     ```
   - Никогда не вписывай сырой ключ в yml. Только `${{ secrets.* }}`.

4. **Разберись с `environment: production`.** Шаблон привязывает каждый job к GitHub Environment `production`.
   - Оставляешь — создай окружение `production` и положи секрет в него (`--env production` у `gh secret set`).
   - Не нужно — удали строку `environment: production` из всех трёх job'ов, тогда возьмётся repository secret.

5. **Проверь целевую ветку.** По умолчанию триггер на PR в любую ветку. Нужен только `main` — добавь `branches: [main]` под `types` в блоке `on.pull_request`.

6. **Убери лишние функции (опционально).** Три job'а независимы. Не нужны предложения по коду — удали job `ai-suggest`. Не нужно описание — удали `ai-describe`. Оставь то, что нужно.

7. **Проверь Actions включены.** В репозитории **Settings → Actions → General** — Actions должны быть разрешены, а workflow permissions позволять запись (конфиг сам запрашивает `contents/issues/pull-requests: write`).

8. **Проверь работу.** Открой тестовый PR (или переоткрой существующий) в целевую ветку. Три job'а должны появиться в **Actions** и отписать комментарии в PR. Если job падает — проверь: имя секрета совпадает, endpoint отвечает, модель существует у провайдера, окружение `production` есть (если оставлено).

## Красные флаги (СТОП)

- Ключ провайдера в открытом виде в yml или в git-истории — только `${{ secrets.* }}`.
- Печать сырого ключа в лог / вывод команды.
- `permissions` шире, чем нужно — трёх write-прав достаточно.

## Матрица значений под шаблон

| Что меняем | Где в yml | Шаблон |
|-----------|-----------|--------|
| Endpoint | `OPENAI_API_BASE` | `https://api.z.ai/api/coding/paas/v4` |
| Модель | `config.model`, `config.model_turbo` | `openai/glm-5` |
| Fallback | `config.fallback_models` | `'["openai/glm-5"]'` |
| Контекст | `config.custom_model_max_tokens` | `128000` |
| Имя ключа | `OPENAI_KEY: ${{ secrets.X }}` | `ZAI_API_KEY` |
| Окружение | `environment:` | `production` |
| Функции | `github_action_config.auto_*` | describe / review / improve |

Справка по всем параметрам: https://qodo-merge-docs.qodo.ai/usage-guide/configuration_options/
