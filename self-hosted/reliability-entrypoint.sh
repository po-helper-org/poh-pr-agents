#!/bin/sh
# Пишет .secrets.toml для pr-agent (как в основном docker-compose.yml), затем
# запускает переданную команду (ingress / worker / sweeper). Секреты — из env
# (см. SETUP.md). Ingress .secrets.toml не использует, но запись безвредна и
# упрощает общий образ.
set -e
mkdir -p /app/pr_agent/settings
{
  printf '[openai]\n'
  printf 'key = "%s"\n' "$OPENAI_KEY"
  printf 'api_base = "%s"\n' "${OPENAI_API_BASE:-https://api.z.ai/api/coding/paas/v4}"
  printf '\n[config]\n'
  printf 'git_provider = "github"\n'
  printf 'model = "%s"\n' "${CONFIG_MODEL:-openai/glm-5}"
  printf 'model_turbo = "%s"\n' "${CONFIG_MODEL:-openai/glm-5}"
  printf 'fallback_models = ["%s", "%s"]\n' "${CONFIG_MODEL:-openai/glm-5}" "${CONFIG_MODEL:-openai/glm-5}"
  printf 'custom_model_max_tokens = %s\n' "${CONFIG_MAX_TOKENS:-128000}"
  printf 'ai_timeout = %s\n' "${CONFIG_AI_TIMEOUT:-90}"
  printf '\n[github]\n'
  printf 'deployment_type = "app"\n'
  printf 'app_id = "%s"\n' "$GITHUB_APP_ID"
  printf 'webhook_secret = "%s"\n' "$GITHUB_WEBHOOK_SECRET"
  printf 'private_key = """\n'
  printf '%s' "$GITHUB_PRIVATE_KEY_B64" | base64 -d
  printf '\n"""\n'
} > /app/pr_agent/settings/.secrets.toml
exec "$@"
