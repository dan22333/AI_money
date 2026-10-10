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

It only turns on when settings.MEMORY_ENABLED is true (the GCP deploy sets it).
Locally and in CI it stays OFF, and a simple in-memory fallback keeps the agent
fully runnable. If mem0 is enabled but anything fails (bad creds, DB down,
provider mismatch), we log once and degrade to the same fallback — a memory
outage must never break a reply.
"""
from __future__ import annotations

from typing import List

from config import settings

_client = None
_fallback: dict[str, List[str]] = {}
_USING_MEM0 = False
_INIT_DONE = False


def _mem0_config() -> dict:
    """Build the self-hosted mem0 config from settings."""
    return {
        "vector_store": {
            "provider": "pgvector",
            "config": {
                "dbname": settings.PG_DB,
                "user": settings.PG_USER,
                "password": settings.PG_PASSWORD,
                "host": settings.PG_HOST,  # /cloudsql/<conn> socket dir on Cloud Run
                "port": 5432,
                "collection_name": "fan_memories",
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


def _init() -> None:
    global _client, _USING_MEM0, _INIT_DONE
    if _INIT_DONE:
        return
    _INIT_DONE = True
    if not settings.MEMORY_ENABLED:
        return  # local/CI: stay on the in-memory fallback
    try:
        from mem0 import Memory
        _client = Memory.from_config(_mem0_config())
        _USING_MEM0 = True
    except Exception as e:  # pragma: no cover - any failure degrades to fallback
        print(f"[memory] mem0 unavailable ({e}); using in-memory fallback.")
        _client = None
        _USING_MEM0 = False


def recall(fan_id: str, query: str, limit: int = 6) -> str:
    """Return a short bullet list of the most relevant remembered facts."""
    _init()
    if _USING_MEM0 and _client is not None:
        try:
            res = _client.search(query=query, user_id=fan_id, limit=limit)
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
    _init()
    if _USING_MEM0 and _client is not None:
        try:
            _client.add(
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
    _init()
    if _USING_MEM0 and _client is not None:
        try:
            _client.add(messages=[{"role": "user", "content": fact}], user_id=fan_id)
            return
        except Exception as e:
            print(f"[memory] add_fact failed: {e}")
    _fallback.setdefault(fan_id, []).append(fact)
