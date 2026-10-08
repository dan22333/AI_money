# Jenny Agent

Autonomous AI chat agent that plays **Jenny Carter** (`persona.md`) on Fanvue.
LangGraph brain · OpenRouter models (Grok 4.3 voice, Mistral-Nemo mode classifier) ·
mem0 memory · deterministic selling · Firestore + (Phase 2) BigQuery.

Full design: `../docs/jenny_system_design.html`.

## Layout

| File | Role |
|---|---|
| `graph.py` | LangGraph agent: ingest → load_context → agent ⇄ tools → persist |
| `modes.py` | Intimacy-mode classifier (COLD/WARM/FLIRTY/EXPLICIT), per turn |
| `tools.py` | `offer_content` (deterministic PPV sell), `send_teaser`, `send_message`, `save_fact` |
| `store.py` | fans · messages · purchases · catalog; eligibility + cooldown + sessions |
| `memory.py` | mem0 durable facts (+ in-memory fallback) |
| `llm.py` | Voice model + classifier seams (injectable for tests) |
| `router.py` | Event router: message→agent, payment→record+react, sub/follow→fan |
| `fanvue_client.py` | Fanvue REST + OAuth refresh-token rotation |
| `main.py` | FastAPI: `/webhook/fanvue`, `/simulate`, `/admin/reconcile`, `/healthz` |
| `simulate.py` | Local end-to-end driver (synthetic fan, dry-run) |
| `tests/` | unit · tool · router · offline e2e (fake LLM) |

## Run locally

```bash
cd agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python simulate.py                      # scripted escalating convo (real models, dry-run)
# or serve it:
uvicorn main:app --reload --port 8080
curl -s localhost:8080/simulate -H 'content-type: application/json' \
     -d '{"text":"hey jenny"}' | python -m json.tool
```

## Test

```bash
pip install -r requirements-dev.txt
ruff check . && pytest        # what CI runs
```

Tests are offline/deterministic — the e2e suite injects a fake voice model, so the
whole graph runs in CI without network or spend, proving wiring + the no-re-sell guarantee.

## Config (`../.env.local` locally; Secret Manager + env in prod)

- `OPENROUTER_API_KEY` · `OPENROUTER_MODEL` (default `x-ai/grok-4.3`) · `MODE_MODEL` (`mistralai/mistral-nemo`)
- `MAX_OUTPUT_TOKENS` (default 600 — keeps credit reservation small)
- `USE_FIRESTORE=true` + `GCP_PROJECT` for persistent storage
- `SIM_SECRET` to protect `/simulate` + `/admin/*`
- Monetization: `PPV_COOLDOWN_MINUTES`, `MAX_OFFERS_PER_SESSION`, `DEFAULT_PPV_PRICE_CENTS`, `SESSION_GAP_MINUTES`

## CI/CD

- `.github/workflows/ci.yml` — ruff + pytest on every push/PR.
- `.github/workflows/deploy.yml` — on green CI on `main`: build → Artifact Registry → Cloud Run (auth via Workload Identity Federation).

## Phase 2 TODO (wiring, not logic)

1. One-time Fanvue browser auth (`../fanvue_auth.py`) → refresh token → Secret Manager.
2. Firestore backend for `store.py` (schema already shaped).
3. Qdrant for mem0; BigQuery export for analytics.
4. Media pipeline (GCS + Eventarc) + `/admin/reconcile` endpoints.
5. Register webhook URL + events in Fanvue.
