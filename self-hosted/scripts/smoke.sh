#!/usr/bin/env bash
# Смоук-проверка reliability-стека после go-live (issue #1).
# Запускать ТАМ, где доступен ingress (на хосте Dokploy или через проброс порта).
# Не деструктивно: только читает /health и /metrics и печатает подсказки к ручным
# шагам (реальный PR, симуляция сбоя). Полная приёмка — в ../GO-LIVE.md.
#
#   BASE_URL=http://127.0.0.1:3000 ./smoke.sh
#
set -euo pipefail
BASE_URL="${BASE_URL:-http://127.0.0.1:3000}"
fail() { echo "❌ $*" >&2; exit 1; }
ok()   { echo "✅ $*"; }

echo "== smoke: reliability ingress @ $BASE_URL =="

# 1) health — ingress жив
code="$(curl -fsS -o /dev/null -w '%{http_code}' "$BASE_URL/health" || true)"
[ "$code" = "200" ] || fail "/health вернул $code (ждали 200) — ingress не поднялся?"
ok "/health 200"

# 2) metrics — экспозиция наблюдаемости (К-5); формат Prometheus
metrics="$(curl -fsS "$BASE_URL/metrics" || true)"
echo "$metrics" | grep -q '^reliability_' || fail "/metrics не отдал reliability_* метрики"
ok "/metrics отдаёт reliability_* (queue_depth, dead_letter_total, gateway_*)"
echo "--- текущие метрики ---"; echo "$metrics" | grep -E 'queue_depth|dead_letter|gateway_|processed_ok|backpressure' || true
echo "-----------------------"

# 3) неподписанный webhook → 401 (СТ-1), НЕ 500
code="$(curl -fsS -o /dev/null -w '%{http_code}' -X POST "$BASE_URL/webhook" \
  -H 'X-GitHub-Event: ping' -d '{}' || true)"
[ "$code" = "401" ] || fail "неподписанный POST /webhook вернул $code (ждали 401 — HMAC СТ-1)"
ok "неподписанный /webhook → 401 (подпись проверяется)"

cat <<'EOF'

== Дальше — РУЧНЫЕ шаги приёмки (см. GO-LIVE.md §Смоук/§Chaos) ==
  1. Открыть тестовый PR в настроенном репо → в течение ~1–2 мин появляется ревью.
     Проверить: /metrics → reliability_processed_ok вырос.
  2. Симулировать сбой LLM (неверный OPENAI_KEY на воркере / недоступный api_base):
     повторить PR → после RELIABILITY_MAX_ATTEMPTS в PR появляется ВИДИМЫЙ коммент
     о провале (не тишина), /metrics → reliability_dead_letter_total вырос.
  3. Chaos: перезапустить контейнер worker в момент анализа → задача передоставлена
     и завершена, дублей ревью/коммента нет (идемпотентность СТ-25).
  4. Убрать ingress на минуту, вернуть → sweeper дозапускает пропущенный PR.
Готово, если все 4 прошли. Иначе — откат: docker-compose.legacy-pr-agent.yml.
EOF
ok "автопроверки smoke пройдены — переходи к ручным шагам выше"
