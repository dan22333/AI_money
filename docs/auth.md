# Fanvue OAuth tokens

Fanvue offers **no API keys / personal tokens / client_credentials** — user-consent
OAuth (PKCE) is the only auth. A one-time browser authorization mints a **refresh
token** (`offline_access` scope); the running service trades it for ~1h access
tokens and keeps itself alive forever by rotating.

## The rotation hazard (why refresh is coordinated)

Refresh tokens are **single-use and rotating**: every refresh consumes the current
token and returns a new one. But Cloud Run runs **many instances** (up to
`maxScale`). If two instances refresh the same token at once, the first rotates it
and **invalidates the token the others — and the persisted secret — still hold**.
Once that happens every refresh returns `400 invalid_grant` and the **whole fleet is
locked out** until a human re-authorizes. This actually happened in prod (last
good rotation sat 5 days with no new secret version while messages bounced).

The fix (`agent/fanvue_client.py` `_refresh` → `_refresh_coordinated`, backed by
`store.acquire_lock`/`release_lock`):

1. **Lock.** On the Secret Manager backend a refresh runs under a cross-instance
   Firestore lock (`locks/fanvue_token_refresh`, pinned to the real DB via
   `store.namespace("real")`), so only one instance rotates at a time. The lock has
   a TTL (`FANVUE_REFRESH_LOCK_TTL`) so a dead holder self-heals.
2. **Re-read & reuse.** Inside the lock we reload the latest persisted token. If a
   sibling already refreshed, its access token is still valid and we **reuse it** —
   no second rotation. This is enabled by persisting an **absolute** expiry
   (`access_expires_at`) so any instance can judge validity, not just the one that
   minted it.
3. **Retry once on 400/401.** If our token lost a rotation race, we reload the
   latest persisted token once and retry with it before failing.
4. **Wait, don't stampede.** An instance that can't win the lock waits
   (`FANVUE_REFRESH_LOCK_WAIT` × `FANVUE_REFRESH_LOCK_TRIES`), re-reading until a
   valid token lands or it falls back to the latest cached one.

The file backend (local dev, single process) skips all of this and just refreshes.

## Recovery runbook — re-mint the token

If the chain is already dead (`400 invalid_grant` on `oauth2/token`, no recent
`fanvue-oauth-tokens` secret version), no code can revive it — a human must re-auth:

```bash
# from repo root; secrets/.env must hold OAUTH_CLIENT_ID + OAUTH_CLIENT_SECRET
OAUTH_REDIRECT_URI=http://localhost:8080/callback python3 scripts/fanvue_auth.py
# → browser consent → writes secrets/.fanvue_tokens.json
gcloud secrets versions add fanvue-oauth-tokens \
  --data-file=secrets/.fanvue_tokens.json --project=capsule-487202
# restart the service so a fresh client loads the new token
gcloud run services update jenny-agent --region us-central1 --project capsule-487202 \
  --update-env-vars _TOKEN_RELOADED=$(date +%s)
```

Verify: `POST /admin/reconcile` with the `x-sim-secret` header returns `ok` (not a
401/400 send failure), or watch the logs for `[webhook] replied` with no
`[send] delivery failed`.

## Settings

| env | default | meaning |
|---|---|---|
| `FANVUE_TOKENS_SOURCE` | `file` | `secret` on GCP (Secret Manager), `file` for local dev |
| `FANVUE_TOKENS_SECRET` | `fanvue-oauth-tokens` | secret holding the rotating token JSON |
| `FANVUE_REFRESH_LOCK_TTL` | `30` | seconds a refresh may hold the cross-instance lock |
| `FANVUE_REFRESH_LOCK_TRIES` | `20` | how many times a waiter re-checks for a sibling's token |
| `FANVUE_REFRESH_LOCK_WAIT` | `0.5` | seconds between those re-checks |
