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

import store
from config import settings

# one mem0 client per namespace ("real" | "sim"); value is None once init failed.
_clients: dict[str, object] = {}
_fallback: dict[str, List[str]] = {}


def _collection_for(namespace: str) -> str:
    base = settings.MEMORY_COLLECTION
    return base if namespace == "real" else f"{base}_sim"


def _mem0_config(collection: str) -> dict:
    """Build the self-hosted mem0 config for one collection."""
    return {
        "vector_store": {
            "provider": "pgvector",
            "config": {
                "dbname": settings.PG_DB,
                "user": settings.PG_USER,
                "password": settings.PG_PASSWORD,
                "host": settings.PG_HOST,  # /cloudsql/<conn> socket dir on Cloud Run
                "port": 5432,
                "collection_name": collection,
                "embedding_model_dims": settings.EMBED_DIM,
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
        _clients[namespace] = Memory.from_config(_mem0_config(_collection_for(namespace)))
    except Exception as e:  # pragma: no cover - any failure degrades to fallback
        print(f"[memory] mem0 unavailable for ns={namespace} ({e}); using in-memory fallback.")
        _clients[namespace] = None
    return _clients[namespace]


def recall(fan_id: str, query: str, limit: int = 6) -> str:
    """Return a short bullet list of the most relevant remembered facts."""
    client = _client_for(store.current_namespace())
    if client is not None:
        try:
            res = client.search(query=query, user_id=fan_id, limit=limit)
            items = res.get("results", res) if isinstance(res, dict) else res
            facts = [i.get("memory") or i.get("text", "") for i in items]
        except Exception as e:
            print(f"[memory] search failed: {e}")
            facts = []
    else:
        facts = _fallback.get(fan_id, [])[-limit:]
    facts = [f for f in facts if f]
    return "\n".join(f"- {f}" for f in facts) if facts else "(nothing remembered yet)"


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


def add_fact(fan_id: str, fact: str) -> None:
    """Explicitly store a single fact (used by the save_fact tool)."""
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
    client.add(messages=[{"role": "user", "content": "memcheck probe: the sky is teal"}],
               user_id=probe_user)
    res = client.search(query="what color is the sky", user_id=probe_user, limit=1)
    items = res.get("results", res) if isinstance(res, dict) else res
    if not items:
        return {"status": "error", "detail": "probe write succeeded but recall returned nothing"}
    return {"status": "ok", "collection": _collection_for(store.current_namespace())}
