"""Long-term fan memory — the "remembers things about you" layer.

SELF-HOSTED mem0 (no SaaS). mem0 runs in-process: it uses an LLM to extract
durable FACTS from conversation turns ("his name is Mike, works night shifts,
has a labrador, into tennis"), embeds them with Vertex AI, and stores the
vectors in our own Cloud SQL Postgres (pgvector). On each new message we
semantically recall the most relevant facts for that fan.

Wiring (all from config/env; no API keys for Google — the runtime SA's ADC):
  - vector store : pgvector on Cloud SQL Postgres (unix socket /cloudsql/...)
  - embedder     : Vertex AI text-embedding-004 (768-dim)
  - fact LLM     : our existing OpenRouter voice model

STAGING ISOLATION (mirrors Firestore): synthetic `sim:` traffic uses a SEPARATE
pgvector collection (<col>_sim) from real fans (<col>), selected per request by
store.current_namespace(). So /simulate + the canary never pollute real fans'
memories, and we can rehearse the memory path against staging rows.

It only turns on when settings.MEMORY_ENABLED is true (the GCP deploy sets it).
Locally and in CI it stays OFF, and a simple in-memory fallback keeps the agent
fully runnable. If mem0 is enabled but anything fails (bad creds, DB down,
provider mismatch), we log once and degrade to the same fallback — a memory
outage must never break a reply. The one exception is selftest(), which is
deliberately loud so the canary can gate on it.
"""
from __future__ import annotations

from typing import List, Optional
from urllib.parse import quote

import store
from config import settings

# one mem0 client per namespace ("real" | "sim"); value is None once init failed.
_clients: dict[str, object] = {}
_fallback: dict[str, List[str]] = {}


def _collection_for(namespace: str) -> str:
    base = settings.MEMORY_COLLECTION
    return base if namespace == "real" else f"{base}_sim"


def _db_for(namespace: str) -> str:
    """real → prod database (PG_DB); sim → the staging database (PG_DB_STAGING) on
    the SAME instance. Separate databases give test memories AND Postgres migrations
    their own schema, so a migration can be rehearsed on staging before prod — the
    pgvector equivalent of Firestore's `staging` DB."""
    return settings.PG_DB if namespace == "real" else settings.PG_DB_STAGING


def _pg_connection_string(db: str | None = None) -> str:
    """libpq URI for the pgvector store.

    mem0's pgvector provider only knows how to build `…@{host}:{port}/…`, which
    CANNOT express a Cloud SQL unix socket: the socket dir (/cloudsql/<conn>) has
    slashes+colons, so `@/cloudsql/…` parses as an EMPTY host and libpq silently
    falls back to its compiled-in default socket (/var/run/postgresql) → connect
    fails. So we hand mem0 a ready-made `connection_string` (it overrides the
    individual params) with the socket dir in the `host=` query param — exactly
    how migrations/env.py connects. The password is percent-encoded so a special
    char in it can't corrupt the URI.
    """
    db = db or settings.PG_DB
    user = quote(settings.PG_USER, safe="")
    pw = quote(settings.PG_PASSWORD, safe="")
    if settings.PG_HOST.startswith("/"):  # unix socket (Cloud Run)
        return (f"postgresql://{user}:{pw}@/{db}"
                f"?host={settings.PG_HOST}")
    return f"postgresql://{user}:{pw}@{settings.PG_HOST}:5432/{db}"


def _mem0_config(collection: str, db: str | None = None) -> dict:
    """Build the self-hosted mem0 config for one collection in database `db`
    (defaults to the prod PG_DB)."""
    db = db or settings.PG_DB
    return {
        "vector_store": {
            "provider": "pgvector",
            "config": {
                "dbname": db,
                "connection_string": _pg_connection_string(db),
                "collection_name": collection,
                "embedding_model_dims": settings.EMBED_DIM,
                # scaling knobs (mem0-native): HNSW index for fast ANN recall +
                # a per-process connection pool. Defaults keep today's behavior.
                "hnsw": settings.MEMORY_USE_HNSW,
                "minconn": settings.MEMORY_PG_MINCONN,
                "maxconn": settings.MEMORY_PG_MAXCONN,
            },
        },
        "embedder": {
            "provider": "vertexai",
            "config": {
                "model": settings.EMBED_MODEL,  # text-embedding-004
                "embedding_dims": settings.EMBED_DIM,
            },
        },
        "llm": {
            "provider": "openai",  # OpenRouter is OpenAI-compatible
            "config": {
                "model": settings.MODEL,
                "openai_base_url": settings.OPENROUTER_BASE_URL,
                "api_key": settings.OPENROUTER_API_KEY,
            },
        },
    }


def _client_for(namespace: str) -> Optional[object]:
    """Return (and cache) the mem0 client for this namespace, or None if memory
    is disabled or failed to initialize (→ caller uses the in-memory fallback)."""
    if not settings.MEMORY_ENABLED:
        return None
    if namespace in _clients:
        return _clients[namespace]
    try:
        from mem0 import Memory
        _clients[namespace] = Memory.from_config(
            _mem0_config(_collection_for(namespace), _db_for(namespace)))
    except Exception as e:  # pragma: no cover - any failure degrades to fallback
        print(f"[memory] mem0 unavailable for ns={namespace} ({e}); using in-memory fallback.")
        _clients[namespace] = None
    return _clients[namespace]


def _search(fan_id: str, query: str, limit: int = 6) -> List[str]:
    """Low-level semantic search → list of fact strings for ONE query (or recent
    fallback facts when mem0 is unavailable). Never raises."""
    client = _client_for(store.current_namespace())
    if client is not None:
        try:
            res = client.search(query=query, filters={"user_id": fan_id}, top_k=limit)
            items = res.get("results", res) if isinstance(res, dict) else res
            return [f for i in items if (f := (i.get("memory") or i.get("text", "")))]
        except Exception as e:
            print(f"[memory] search failed: {e}")
            return []
    return [f for f in _fallback.get(fan_id, [])[-limit:] if f]


def _format(facts: List[str]) -> str:
    facts = [f for f in facts if f]
    return "\n".join(f"- {f}" for f in facts) if facts else "(nothing remembered yet)"


def recall(fan_id: str, query: str, limit: int = 6) -> str:
    """Return a short bullet list of the most relevant remembered facts (single
    query — the latest-message strategy). Kept for the simple callers."""
    return _format(_search(fan_id, query, limit))


def _join_turns(recent_turns: List[dict], incoming: str = "") -> str:
    """Flatten the last few turns (+ the current message) into one query string, so
    recall has conversational CONTEXT to match on — not just a bare 'hey'."""
    lines = [f"{t.get('role', 'fan')}: {t.get('text', '')}".strip()
             for t in recent_turns if (t.get("text") or "").strip()]
    if incoming:
        lines.append(f"fan: {incoming}")
    return "\n".join(lines)


_EXPAND_SYSTEM = (
    "You help a chat AI recall what it already knows about a fan. Given the recent "
    "conversation, write 2-4 SHORT hypothetical statements describing the KINDS of "
    "facts probably worth recalling about THIS fan, phrased the way a stored memory "
    "note would be (e.g. \"the fan works night shifts in healthcare\", \"the fan has "
    "a pet dog\", \"the fan is planning a trip soon\"). These are search probes, so: "
    "generalize — capture the kind of fact and keep specifics vague — do NOT copy the "
    "fan's exact words, and NEVER invent details the fan didn't imply. Output a JSON "
    "object {\"queries\": [...]}. If nothing is worth recalling, return {\"queries\": []}."
)


def expand_queries(context_text: str, max_queries: int = 3) -> List[str]:
    """Cheap LLM query-expansion: turn the recent conversation into a few GENERAL
    retrieval probes (topics, not verbatim echoes) so the vector search matches
    stored facts even when wording differs and doesn't overfit to the last line.
    Returns [] on any failure (caller falls back to the plain context query)."""
    if not (context_text or "").strip():
        return []
    import llm
    try:
        out = llm.classify_json(_EXPAND_SYSTEM, context_text)
        qs = out.get("queries") if isinstance(out, dict) else None
        return [str(q).strip() for q in (qs or []) if str(q).strip()][:max_queries]
    except Exception as e:
        print(f"[memory] query expansion failed: {e}")
        return []


def _recall_expanded(fan_id: str, probes: List[str], limit: int) -> List[str]:
    """Search each probe (up to `limit` facts each), concat in probe order, de-dupe,
    and cap the whole set at RECALL_MAX_FACTS so the prompt doesn't bloat.

    Sequential ON PURPOSE: every probe goes through the one cached mem0 client,
    which holds a single pgvector/psycopg2 connection that is NOT safe for
    concurrent queries — parallel searches would serialize on it or silently error.
    The cost is small (~0.2s/probe) and off the critical path (this whole call runs
    in the recall thread, parallel to classify, and the voice model dwarfs it)."""
    facts: List[str] = []
    seen: set = set()
    for p in probes:
        for f in _search(fan_id, p, limit):
            if f not in seen:
                seen.add(f)
                facts.append(f)
    return facts[:settings.RECALL_MAX_FACTS]


def recall_contextual(fan_id: str, incoming: str, recent_turns: List[dict], *,
                      strategy: str = "latest", limit: int = 6) -> str:
    """Strategy-dispatched recall used by the live agent:
      - 'latest'  : embed the latest message only (legacy)
      - 'context' : embed the last few turns joined (fixes the bare-'hey' query)
      - 'expanded': LLM → general probes → union of searches (sharpest, +1 fast call)
    Falls back a level on empty input so it's always safe."""
    ctx = _join_turns(recent_turns, incoming)
    if strategy == "context":
        return _format(_search(fan_id, ctx or incoming, limit))
    if strategy == "expanded":
        probes = expand_queries(ctx, settings.RECALL_EXPANSION_PROBES) or [ctx or incoming]
        return _format(_recall_expanded(fan_id, probes, limit))
    return _format(_search(fan_id, incoming or ctx, limit))  # 'latest'


def recall_eval(fan_id: str, incoming: str, recent_turns: List[dict], limit: int = 6) -> dict:
    """Read-only comparison of all three strategies for the SAME input — powers the
    /admin/recall_eval endpoint so recall quality can be eyeballed against the real
    memory store before we switch RECALL_STRATEGY. No writes, no side effects."""
    ctx = _join_turns(recent_turns, incoming)
    probes = expand_queries(ctx, settings.RECALL_EXPANSION_PROBES)
    return {
        "latest": {"query": incoming, "facts": _search(fan_id, incoming, limit)},
        "context": {"query": ctx, "facts": _search(fan_id, ctx or incoming, limit)},
        "expanded": {"probes": probes,
                     "facts": _recall_expanded(fan_id, probes or [ctx or incoming], limit)},
    }


def add_turn(fan_id: str, fan_text: str, jenny_text: str) -> None:
    """Feed a conversation turn so mem0 can extract/update durable facts."""
    client = _client_for(store.current_namespace())
    if client is not None:
        try:
            client.add(
                messages=[
                    {"role": "user", "content": fan_text},
                    {"role": "assistant", "content": jenny_text},
                ],
                user_id=fan_id,
            )
            return
        except Exception as e:
            print(f"[memory] add failed: {e}")
    _fallback.setdefault(fan_id, []).append(f"fan said: {fan_text}")


def backfill(fan_id: str, turns: List[dict]) -> int:
    """One-time fact extraction over backfilled chat history.

    `turns` is a chronological list of {"role": "fan"|"jenny", "text": str},
    pre-filtered to non-empty text by the caller. Unlike add_turn (one live
    exchange), this feeds many stored messages in batches of
    settings.MEMORY_BACKFILL_CHUNK per mem0 add() call, so the number of
    extraction-LLM calls scales with history/chunk rather than per message — the
    bit that actually costs money/time when a fan has a long backlog.

    Returns the number of LEADING turns successfully fed (contiguous from the
    start). On the first chunk whose add() fails we STOP and return the count so
    far — we do NOT skip ahead — so the caller can advance its watermark only as
    far as what actually landed in mem0; the un-fed tail is retried next run
    instead of being silently dropped. No-op (0) when memory is disabled or the
    client failed to init, so backfill is free locally/in CI.
    """
    if not settings.MEMORY_ENABLED:
        return 0
    client = _client_for(store.current_namespace())
    if client is None:
        return 0
    fed = 0
    size = max(1, settings.MEMORY_BACKFILL_CHUNK)
    for i in range(0, len(turns), size):
        batch = [{"role": "assistant" if t.get("role") == "jenny" else "user",
                  "content": t["text"]} for t in turns[i:i + size]]
        try:
            client.add(messages=batch, user_id=fan_id)
        except Exception as e:  # degrade: stop BEFORE the watermark can pass unsaved facts
            print(f"[memory] backfill add failed for {fan_id} after {fed} turns: {e}")
            break
        fed += len(batch)
    return fed


def add_fact(fan_id: str, fact: str) -> None:
    """Explicitly store a single fact. No longer exposed as an agent tool (mem0
    extracts facts automatically every turn via add_turn); kept for tests and any
    direct/programmatic fact seeding."""
    client = _client_for(store.current_namespace())
    if client is not None:
        try:
            client.add(messages=[{"role": "user", "content": fact}], user_id=fan_id)
            return
        except Exception as e:
            print(f"[memory] add_fact failed: {e}")
    _fallback.setdefault(fan_id, []).append(fact)


def selftest() -> dict:
    """Force a real write+read roundtrip in the CURRENT namespace and report.

    Unlike recall/add, this does NOT swallow failures — the canary calls it (via
    /admin/memcheck in the sim namespace) so a silently-broken memory layer
    blocks promotion instead of shipping. Returns {"status": ok|disabled|error}.
    """
    if not settings.MEMORY_ENABLED:
        return {"status": "disabled"}
    client = _client_for(store.current_namespace())
    if client is None:
        return {"status": "error", "detail": "mem0 client failed to initialize"}
    probe_user = "sim:__memcheck__"
    # infer=False stores the probe VERBATIM — a healthcheck must exercise the
    # embed+store+search plumbing deterministically, not the fact-extraction LLM's
    # judgment (with infer=True it may decide the probe isn't worth remembering,
    # so add "succeeds" having stored nothing and recall comes back empty).
    client.add(messages=[{"role": "user", "content": "memcheck probe: the sky is teal"}],
               user_id=probe_user, infer=False)
    res = client.search(query="what color is the sky", filters={"user_id": probe_user}, top_k=1)
    items = res.get("results", res) if isinstance(res, dict) else res
    if not items:
        return {"status": "error", "detail": "probe write succeeded but recall returned nothing"}
    return {"status": "ok", "collection": _collection_for(store.current_namespace())}
