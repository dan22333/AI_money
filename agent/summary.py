"""Rolling conversation summary — the "story so far" shown in the system prompt.

The recent-messages window (graph.load_context) only carries the last ~15 turns.
For long relationships we keep a compact, continuously-updated prose summary on
the fan record (fan["rollingSummary"]) so Jenny never loses the thread of who
the fan is and what's happened between them.

It refreshes at most once every SUMMARIZE_AFTER messages (cheap classifier model),
and never lets a summarization failure break a reply.
"""
from __future__ import annotations

from typing import List

import llm
from config import settings

_SYSTEM = (
    "You maintain a concise running summary of an ongoing chat between an adult "
    "creator (Jenny) and a fan. Fold the latest exchange into the EXISTING summary. "
    "Keep it under 120 words, third person, factual: who the fan is, what they like, "
    "relationship warmth, what's happened, any promises made. No preamble — output "
    "ONLY the updated summary."
)


def should_refresh(message_count: int, last_summarized_at: int) -> bool:
    """True once at least SUMMARIZE_AFTER new messages have accrued since last time."""
    return (message_count - last_summarized_at) >= settings.SUMMARIZE_AFTER


def build(previous: str, history: List[dict]) -> str:
    """Produce an updated rolling summary from the previous one + recent turns."""
    convo = "\n".join(f"{m.get('role')}: {m.get('text','')}" for m in history[-settings.SUMMARIZE_AFTER:])
    user = (f"EXISTING SUMMARY:\n{previous or '(none yet)'}\n\n"
            f"LATEST CONVERSATION:\n{convo}\n\nUpdated summary:")
    return llm.complete(_SYSTEM, user)


def maybe_update(fan: dict, history: List[dict], message_count: int) -> bool:
    """Refresh fan['rollingSummary'] in place if due. Returns True if it changed.

    Caller is responsible for persisting the fan afterwards. Never raises.
    """
    last = int(fan.get("summarizedAtCount", 0))
    if not should_refresh(message_count, last):
        return False
    try:
        fan["rollingSummary"] = build(fan.get("rollingSummary", ""), history)
        fan["summarizedAtCount"] = message_count
        return True
    except Exception as e:  # never break a reply over a summary refresh
        print(f"[summary] refresh failed: {e}")
        return False
