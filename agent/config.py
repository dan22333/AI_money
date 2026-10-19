"""Central configuration. Loads .env.local for local dev; on GCP, values come
from real environment variables / Secret Manager."""
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


# Local convenience only — never commit these (secrets/ is gitignored).
_load_env_file(REPO_ROOT / "secrets" / ".env")
_load_env_file(REPO_ROOT / ".env.local")  # legacy fallback


class Settings:
    # --- LLM (OpenRouter, OpenAI-compatible) ---
    OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
    OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    # Voice model (explicit-capable, tool-calling). Override via env to A/B test.
    MODEL = os.environ.get("OPENROUTER_MODEL", "x-ai/grok-4.3")
    TEMPERATURE = float(os.environ.get("MODEL_TEMPERATURE", "0.9"))
    MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "600"))
    # Fast, non-reasoning, permissive classifier for intimacy mode.
    MODE_MODEL = os.environ.get("MODE_MODEL", "mistralai/mistral-nemo")

    # --- Monetization policy ---
    SESSION_GAP_MINUTES = int(os.environ.get("SESSION_GAP_MINUTES", "45"))
    PPV_COOLDOWN_MINUTES = int(os.environ.get("PPV_COOLDOWN_MINUTES", "20"))
    MAX_OFFERS_PER_SESSION = int(os.environ.get("MAX_OFFERS_PER_SESSION", "2"))
    DEFAULT_PPV_PRICE_CENTS = int(os.environ.get("DEFAULT_PPV_PRICE_CENTS", "1000"))
    SUMMARIZE_AFTER = int(os.environ.get("SUMMARIZE_AFTER", "20"))

    # --- Persona ---
    PERSONA_PATH = os.environ.get("PERSONA_PATH", str(Path(__file__).resolve().parent / "persona.md"))

    # --- Long-term memory: SELF-HOSTED mem0 on our own Postgres/pgvector (no SaaS) ---
    # mem0 runs in-process; facts embedded by Vertex AI (runtime SA, no key) and
    # stored as vectors in Cloud SQL Postgres. Disabled by default (local/tests);
    # the deploy turns it on. If anything fails it degrades to a no-op.
    MEMORY_ENABLED = os.environ.get("MEMORY_ENABLED", "false").lower() == "true"
    PG_INSTANCE = os.environ.get("PG_INSTANCE", "capsule-487202:us-central1:jenny-pg")
    # Cloud Run reaches Cloud SQL over a unix socket at /cloudsql/<connection-name>.
    PG_HOST = os.environ.get("PG_HOST", f"/cloudsql/{PG_INSTANCE}")
    PG_DB = os.environ.get("PG_DB", "jenny")
    # Separate staging DATABASE on the SAME instance for sim: traffic — isolates
    # test memories AND schema/migrations from prod (mirrors Firestore's staging DB).
    # The `sim` namespace's mem0 client connects here; `real` uses PG_DB. A separate
    # *instance* (full server isolation) is the later upgrade and needs only this
    # name repointed. See memory._db_for.
    PG_DB_STAGING = os.environ.get("PG_DB_STAGING", "jenny_staging")
    PG_USER = os.environ.get("PG_USER", "postgres")
    PG_PASSWORD = os.environ.get("PG_PASSWORD", "")
    EMBED_MODEL = os.environ.get("EMBED_MODEL", "text-embedding-004")
    EMBED_DIM = int(os.environ.get("EMBED_DIM", "768"))
    VERTEX_REGION = os.environ.get("VERTEX_REGION", "us-central1")
    # pgvector scaling (mem0 supports both natively). HNSW = approximate-nearest-
    # neighbour index so recall stays fast as total fact-vectors grow (mem0 runs
    # CREATE INDEX IF NOT EXISTS ... USING hnsw on create_col — idempotent). OFF by
    # default; turn on together with a DB tier that has RAM to hold the index.
    # minconn/maxconn size mem0's per-process connection pool — keep maxconn ×
    # max Cloud Run instances under the DB's max_connections (or front with a pooler).
    MEMORY_USE_HNSW = os.environ.get("MEMORY_USE_HNSW", "false").lower() == "true"
    MEMORY_PG_MINCONN = int(os.environ.get("MEMORY_PG_MINCONN", "1"))
    MEMORY_PG_MAXCONN = int(os.environ.get("MEMORY_PG_MAXCONN", "5"))
    # Extraction fan-out: when true, the live reply path + reconcile ENQUEUE a
    # Cloud Tasks job for mem0 fact-extraction instead of running it inline, so the
    # (slow, LLM-bound) learning never blocks a reply or a reconcile run. OFF by
    # default → inline (today's behavior). Needs the jenny-extract queue + the
    # same SERVICE_BASE_URL plumbing as debounce.
    MEMORY_FANOUT_ENABLED = os.environ.get("MEMORY_FANOUT_ENABLED", "false").lower() == "true"
    EXTRACT_TASKS_QUEUE = os.environ.get("EXTRACT_TASKS_QUEUE", "jenny-extract")
    # Base pgvector collection for real fans; sim traffic uses "<base>_sim"
    # (staging isolation, mirrors the Firestore named-DB split).
    MEMORY_COLLECTION = os.environ.get("MEMORY_COLLECTION", "fan_memories")
    # mem0 backfill (reconcile → long-term memory): messages per extraction-LLM
    # call (one add() per chunk, so cost scales with history/chunk, not history),
    # and the max messages a single reconcile run extracts per fan — hitting the
    # cap just advances the `mem:` watermark so the next run resumes.
    MEMORY_BACKFILL_CHUNK = int(os.environ.get("MEMORY_BACKFILL_CHUNK", "20"))
    MEMORY_BACKFILL_LIMIT = int(os.environ.get("MEMORY_BACKFILL_LIMIT", "500"))
    # Recall strategy for the live agent (compare them read-only via /admin/recall_eval
    # before switching). "latest" = embed the last message only (legacy, default);
    # "context" = embed the last RECALL_CONTEXT_TURNS turns; "expanded" = a cheap LLM
    # turns the context into general probes, then search on those (+1 fast call).
    RECALL_STRATEGY = os.environ.get("RECALL_STRATEGY", "latest")
    RECALL_CONTEXT_TURNS = int(os.environ.get("RECALL_CONTEXT_TURNS", "4"))
    RECALL_EXPANSION_PROBES = int(os.environ.get("RECALL_EXPANSION_PROBES", "3"))
    # "expanded": each probe fetches up to RECALL_LIMIT facts (searched sequentially —
    # the pgvector connection isn't safe for concurrent queries); the deduped concat
    # of all probes is then capped here so the prompt stays tight.
    RECALL_LIMIT = int(os.environ.get("RECALL_LIMIT", "6"))
    RECALL_MAX_FACTS = int(os.environ.get("RECALL_MAX_FACTS", "10"))

    # --- Message store (Firestore) ---
    GCP_PROJECT = os.environ.get("GCP_PROJECT", "capsule-487202")
    USE_FIRESTORE = os.environ.get("USE_FIRESTORE", "false").lower() == "true"
    # Named databases (same project). Real fans use the default DB; synthetic
    # `sim:` fans (/simulate, the canary smoke test) use a separate staging DB so
    # test traffic never pollutes production data. See store.use_namespace_for().
    FIRESTORE_DATABASE = os.environ.get("FIRESTORE_DATABASE", "(default)")
    SIM_FIRESTORE_DATABASE = os.environ.get("SIM_FIRESTORE_DATABASE", "staging")

    # --- Fanvue ---
    FANVUE_API_BASE = os.environ.get("API_BASE_URL", "https://api.fanvue.com")
    FANVUE_TOKENS_FILE = os.environ.get("FANVUE_TOKENS_FILE", str(REPO_ROOT / "secrets" / ".fanvue_tokens.json"))
    OAUTH_CLIENT_ID = os.environ.get("OAUTH_CLIENT_ID", "")
    OAUTH_CLIENT_SECRET = os.environ.get("OAUTH_CLIENT_SECRET", "")
    OAUTH_TOKEN_URL = os.environ.get("OAUTH_TOKEN_URL", "https://auth.fanvue.com/oauth2/token")
    FANVUE_WEBHOOK_SECRET = os.environ.get("FANVUE_WEBHOOK_SECRET", "")
    # OAuth token persistence: "file" (local dev) or "secret" (Secret Manager, on GCP).
    # The refresh token rotates on every use, so on GCP we read/write it as new
    # versions of the FANVUE_TOKENS_SECRET secret. The secret VALUE is created
    # out-of-band by the one-time OAuth browser flow (scripts/fanvue_auth.py).
    # "file" (local dev) | "secret" (Secret Manager) | "firestore" (a single
    # overwrite-in-place doc per creator — no version churn, multi-creator-ready;
    # recommended on GCP). On first "firestore" load the doc is seeded from the
    # Secret Manager secret so cutover is automatic.
    FANVUE_TOKENS_SOURCE = os.environ.get("FANVUE_TOKENS_SOURCE", "file")
    FANVUE_TOKENS_SECRET = os.environ.get("FANVUE_TOKENS_SECRET", "fanvue-oauth-tokens")
    # Logical creator the global client authorizes as (the token doc key). One
    # creator today; multi-creator later keys tokens by real creator uuids.
    FANVUE_CREATOR_ID = os.environ.get("FANVUE_CREATOR_ID", "default")
    # Proactive refresher (POST /admin/refresh_tokens, driven by Cloud Scheduler):
    # refresh a token when it's within this many seconds of expiry. 30 min sits
    # under the ~60-min access-token life so each ~30-min cron keeps it valid AND
    # keeps the single-use refresh token from idling out during quiet periods.
    FANVUE_REFRESH_SKEW_SECONDS = float(os.environ.get("FANVUE_REFRESH_SKEW_SECONDS", "1800"))
    # Catalog pricing by heat (cents), applied during backfill. WARM(2) = free (the
    # teaser/hook you send to entice); FLIRTY(3) + EXPLICIT(4) are the paid tiers.
    # Tune without code via env. heat 4 (most explicit) is the top price.
    PRICE_WARM_CENTS = int(os.environ.get("PRICE_WARM_CENTS", "0"))         # free teaser
    PRICE_FLIRTY_CENTS = int(os.environ.get("PRICE_FLIRTY_CENTS", "3000"))  # $30
    PRICE_EXPLICIT_CENTS = int(os.environ.get("PRICE_EXPLICIT_CENTS", "5000"))  # $50
    # Fanvue refresh tokens are SINGLE-USE + rotating, but Cloud Run runs many
    # instances. Without coordination, two instances refresh the same token at
    # once → the first rotates it, invalidating the token the others (and the
    # persisted secret) still hold → the whole fleet gets 400 invalid_grant until
    # a human re-auths. So on the "secret" backend a refresh runs under a
    # cross-instance Firestore lock, re-reads the latest persisted token inside the
    # lock (reusing a sibling's fresh token instead of rotating again), and retries
    # once on 400/401 after reloading. See store.acquire_lock / fanvue_client._refresh.
    FANVUE_REFRESH_LOCK_TTL = int(os.environ.get("FANVUE_REFRESH_LOCK_TTL", "30"))
    FANVUE_REFRESH_LOCK_TRIES = int(os.environ.get("FANVUE_REFRESH_LOCK_TRIES", "20"))
    FANVUE_REFRESH_LOCK_WAIT = float(os.environ.get("FANVUE_REFRESH_LOCK_WAIT", "0.5"))

    # Protects /simulate and /admin endpoints
    SIM_SECRET = os.environ.get("SIM_SECRET", "")

    # --- Per-fan debounce (batch rapid-fire messages into ONE reply) ---
    # OFF by default: when false the webhook replies synchronously exactly as
    # before, so prod is untouched until this is flipped on. When true, a fan
    # message is recorded + a delayed Cloud Tasks job is enqueued; the job
    # (/admin/process_fan) waits for the fan to go quiet, then answers the whole
    # unanswered batch in one reply under a per-fan lock. See docs/debounce.
    DEBOUNCE_ENABLED = os.environ.get("DEBOUNCE_ENABLED", "false").lower() == "true"
    # Quiet window: a batch is only answered once no new message has arrived for
    # this many seconds (each new message pushes the answer out by re-enqueuing).
    # Total time-to-reply ≈ this + generation (~3-6s). ~3s batches rapid-fire bursts
    # while staying responsive; raise it if fans type in slow multi-message spurts.
    DEBOUNCE_SETTLE_SECONDS = float(os.environ.get("DEBOUNCE_SETTLE_SECONDS", "3"))
    # How long a worker may hold the per-fan processing lease before it's assumed
    # dead and a retry may steal it. Must exceed worst-case generation time.
    PROCESS_LEASE_TTL_SECONDS = int(os.environ.get("PROCESS_LEASE_TTL_SECONDS", "90"))
    # When a worker finds the fan busy (lease held), it re-checks after this delay.
    PROCESS_BUSY_RETRY_SECONDS = float(os.environ.get("PROCESS_BUSY_RETRY_SECONDS", "5"))
    # Cloud Tasks queue that drives the worker. Queue + location + the service's
    # own URL (so the task can POST back to /admin/process_fan) + the SA that
    # signs the task's OIDC token. If TASKS_QUEUE or SERVICE_BASE_URL is empty we
    # can't enqueue, so the webhook falls back to the synchronous path.
    TASKS_QUEUE = os.environ.get("TASKS_QUEUE", "jenny-process")
    TASKS_LOCATION = os.environ.get("TASKS_LOCATION", os.environ.get("GCP_REGION", "us-central1"))
    SERVICE_BASE_URL = os.environ.get("SERVICE_BASE_URL", "")
    TASKS_INVOKER_SA = os.environ.get("TASKS_INVOKER_SA", "")


settings = Settings()
