# Secrets

All sensitive values live here **locally** in `secrets/.env` (gitignored). In
production they live in **GCP Secret Manager** — nothing secret is ever committed.

## Files
- `.env.example` — template (committed). Copy to `.env` and fill in.
- `.env` — real local values (gitignored).
- `.fanvue_tokens.json` — Fanvue OAuth tokens from `fanvue_auth.py` (gitignored).

## What each secret is, and its Secret Manager name in prod

| Local key (`secrets/.env`) | Secret Manager name | Purpose | Status |
|---|---|---|---|
| `OPENROUTER_API_KEY` | `openrouter-api-key` | LLM calls (Grok + Mistral) | ✅ have |
| `OAUTH_CLIENT_ID` | (env var, not secret) | Fanvue app id | ✅ have |
| `OAUTH_CLIENT_SECRET` | `fanvue-client-secret` | Fanvue OAuth | ✅ have |
| `FANVUE_WEBHOOK_SECRET` | `fanvue-webhook-secret` | verify webhooks | ⏳ after deploy |
| `SIM_SECRET` | `sim-secret` | guard `/simulate`+`/admin` | ⏳ generate |
| (refresh token) | `fanvue-refresh-token` | 24/7 Fanvue access | ⏳ after browser auth |

## Rotate before production
The OpenRouter key and Fanvue client secret were pasted in a chat during setup —
regenerate them and store the fresh values only here / in Secret Manager.

## Push a secret to GCP Secret Manager (you run it — value never leaves your shell)
```bash
printf "%s" "THE_VALUE" | gcloud secrets create <name> --data-file=- --project=capsule-487202
# update later:
printf "%s" "THE_VALUE" | gcloud secrets versions add <name> --data-file=- --project=capsule-487202
```
