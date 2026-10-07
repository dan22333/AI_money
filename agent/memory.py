"""Long-term fan memory via mem0 — the 'remembers things about you' layer.

mem0 takes conversation turns, uses an LLM to extract durable FACTS
("his name is Mike, works night shifts, has a labrador, into tennis"),
stores them in a vector DB, and lets us semantically recall the relevant
ones for a given fan on each new message.

If mem0 isn't configured yet, a simple in-memory fallback keeps the agent
runnable so you can develop the rest of the pipeline.
"""
from __future__ import annotations

from typing import List

from config import settings

_client = None
_fallback: dict[str, List[str]] = {}
_USING_MEM0 = False


def _init():
    global _client, _USING_MEM0
    if _client is not None or _USING_MEM0:
        return
    try:
        if settings.MEM0_API_KEY:
            from mem0 import MemoryClient  # hosted platform
            _client = MemoryClient(api_key=settings.MEM0_API_KEY)
        else:
            # Self-hosted mem0, using OpenRouter for extraction. Embeddings use a
            # local HF model so no extra provider key is needed for v1.
            from mem0 import Memory
            _client = Memory.from_config({
                "llm": {
                    "provider": "openai",
                    "config": {
                        "model": settings.MODEL,
                        "openai_base_url": settings.OPENROUTER_BASE_URL,
                        "api_key": settings.OPENROUTER_API_KEY,
                    },
                },
                "embedder": {
                    "provider": "huggingface",
                    "config": {"model": "sentence-transformers/all-MiniLM-L6-v2"},
                },
            })
        _USING_MEM0 = True
    except Exception as e:  # pragma: no cover - fall back gracefully
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
