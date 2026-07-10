# Self-hosted PR-Agent (GLM-5) as a GitHub App — Dokploy runbook

Runs [PR-Agent](https://github.com/qodo-ai/pr-agent) as a **webhook GitHub App**
on your own box, with **GLM-5 via Z.AI**. Use this instead of the GitHub Actions
workflow when Actions can't run — e.g. the account is **billing-locked**
(`"account is locked due to a billing issue"`), out of minutes, or blocked by
org policy. The webhook path uses only the GitHub API, so none of that applies.

You get, on every PR: auto **describe / review / improve** on open, re-review on
push, and **command replies** (`/ask`, `/review`, `/improve`, `/describe`) typed
as PR comments — real-time, all from one small container.

> ⚠️ A self-hosted **Actions runner does NOT bypass a billing lock** — the lock
> disables Actions orchestration itself, so jobs never dispatch. Only a
> webhook App (this) truly sidesteps it.

---

## One-shot setup

Prereqs: a Dokploy instance, a hostname that resolves to it, and a Z.AI GLM-5 key.

**1. Create the GitHub App** — open [`register-app.html`](register-app.html) in a
browser, enter `owner/repo` and your webhook base URL (e.g.
`http://pr-agent.sub.traefik.me` — see the traefik.me note below), click
**Create GitHub App**. Copy the `code` from the redirect URL, then:

```bash
bash finish-registration.sh <code>      # writes dokploy.env
```

Edit `dokploy.env` and fill `OPENAI_KEY` with your Z.AI key.

**2. Deploy on Dokploy** — Create Service → **Compose**:
- Provider: Git → this repo, path `self-hosted/docker-compose.yml`
- **Environment**: paste the 4 lines from `dokploy.env`
- **Domains**: Host = your webhook host, **Container Port `3000`**, HTTPS as
  appropriate (see traefik.me note)
- **Deploy**

**3. Install the App** on your repo — the install link is printed by
`finish-registration.sh` (`https://github.com/apps/<slug>/installations/new`).

**4. Add [`.pr_agent.toml`](.pr_agent.toml) to your repo root** (controls language,
focus, which tools auto-run). Edits apply live, no redeploy.

**5. Verify** — open a test PR → `describe/review/improve` appear within a minute;
comment `/review` → it re-runs. Done.

---

## The gotchas (all of these bit us — the config already handles them)

| # | Symptom | Cause | Handled by |
|---|---------|-------|-----------|
| 1 | Every GHA job fails in ~3s | account **billing-locked** — disables Actions entirely (incl. self-hosted runners) | use this webhook App, not Actions |
| 2 | Can't script App creation | GitHub blocks App creation via token/API | browser **manifest flow** (`register-app.html` + conversion API) |
| 3 | `has no attribute 'app_id'` / wrong model | **dotted env vars** (`OPENAI.KEY`, `CONFIG.MODEL`, `GITHUB.APP_ID`) are ignored in this image | entrypoint writes them into `.secrets.toml` |
| 4 | `app_id` vanishes after adding key | pr-agent **replaces a `[section]` wholesale** when loading `.secrets.toml` | write **all** keys of each section, not partial |
| 5 | model/endpoint override ignored | `[config]` / `[openai]` are **host-restricted** — can't be set from repo `.pr_agent.toml` | set them host-side in `.secrets.toml` |
| 6 | `Could not parse the provided public key` | multiline **PEM flattened** by env substitution | pass key as **base64 one line**, decode in entrypoint |
| 7 | `Settings file not found: .../.secrets.toml` | Dokploy **File Mount** doesn't reach the container path in Compose mode | entrypoint **writes the file** itself |
| 8 | webhook `500 tls: certificate is valid for ...traefik.default` / `308` redirect | **traefik.me is HTTP-only**, no TLS | webhook URL `http://`, **disable HTTPS/redirect** on the Dokploy domain, redeploy |
| 9 | `OpenAIException - Country, region, or territory not supported` | default model hit **api.openai.com**, region-blocked | GLM-5 via Z.AI (`api_base`), set in `.secrets.toml` |

### traefik.me note
`*.traefik.me` resolves to the IP embedded in the hostname and serves **plain
HTTP only** (its HTTPS is a self-signed `TRAEFIK DEFAULT CERT`). So:
- GitHub App **Webhook URL** must be `http://...`, not `https://`.
- In the Dokploy domain, turn **HTTPS off** (and any force-redirect), else HTTP
  `308`-redirects to the broken TLS and GitHub can't deliver. **Redeploy** after
  toggling so Traefik rebuilds the router.
- The webhook payload is still HMAC-signed with your webhook secret, so
  authenticity holds; only transport encryption is skipped. For production, put a
  real domain + valid cert in front instead.

## Required Dokploy env

| var | required | default |
|-----|:--:|---|
| `OPENAI_KEY` | ✅ | — |
| `GITHUB_APP_ID` | ✅ | — |
| `GITHUB_WEBHOOK_SECRET` | ✅ | — |
| `GITHUB_PRIVATE_KEY_B64` | ✅ | — |
| `OPENAI_API_BASE` | | `https://api.z.ai/api/coding/paas/v4` |
| `CONFIG_MODEL` | | `openai/glm-5` |
| `CONFIG_MAX_TOKENS` | | `128000` |

## Troubleshooting

- **Webhook delivery red in App → Advanced → Recent Deliveries**: check the
  status. `500 tls:` → gotcha #8. `403/401` → webhook secret mismatch. `200` but
  no comment → the app accepted it; check container logs for the real error.
- **Container logs**: on start you should NOT see `Settings file not found:
  /app/pr_agent/settings/.secrets.toml` (only the harmless `settings_prod` one).
- **`Country not supported`**: `OPENAI_API_BASE`/`CONFIG_MODEL` didn't apply →
  you're on a stale image; redeploy so the entrypoint rewrites `.secrets.toml`.
- **Diagnose auth from your laptop**: with the App ID + `.pem` you can mint an app
  JWT and query `GET /app`, `GET /app/installations`, `GET /app/hook/deliveries`
  to see exactly what GitHub sent and what the server returned.
