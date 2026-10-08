"""Intimacy-mode classifier — a fast, cheap, permissive model call, every turn.

Modes: COLD < WARM < FLIRTY < EXPLICIT. Escalates only as fast as the fan;
de-escalates when they cool. A new session resets the baseline (handled in graph.ingest).
"""
from __future__ import annotations

from typing import List

import llm

VALID = {"COLD", "WARM", "FLIRTY", "EXPLICIT"}

SYSTEM = """You are the intimacy-mode classifier for an adult (18+) creator chat agent.
Given the recent conversation and the CURRENT mode, output the mode for the NEXT reply.

Modes (heat):
  COLD     - brand new / greetings only. welcome, learn name. no flirt push.
  WARM     - friendly rapport, light flirting. default resting state.
  FLIRTY   - clearly flirtatious, suggestive, teasing.
  EXPLICIT - fan is being sexual/explicit; explicit language & NSFW allowed.

Rules:
- Escalate ONLY as fast as the FAN does. Never jump ahead of the fan.
- De-escalate if the fan cools, gets shy, or switches to a casual topic.
- Move at most one step per message.
Respond with ONLY compact JSON:
{"mode":"...","escalate_ok":bool,"monetize_ok":bool,"reason":"short"}"""


def normalize(raw: dict, current_mode: str) -> dict:
    mode = str(raw.get("mode", current_mode)).upper()
    if mode not in VALID:
        mode = current_mode
    return {
        "mode": mode,
        "escalate_ok": bool(raw.get("escalate_ok", False)),
        "monetize_ok": bool(raw.get("monetize_ok", False)),
        "reason": str(raw.get("reason", ""))[:200],
    }


def classify(recent_msgs: List[dict], current_mode: str) -> dict:
    """Decide the mode for the next reply. recent_msgs: [{'role','text'},...]."""
    convo = "\n".join(f"{m['role']}: {m['text']}" for m in recent_msgs[-8:])
    user = (f"CURRENT MODE: {current_mode}\n\nRECENT CONVERSATION:\n{convo}\n\n"
            "Classify the mode for the next reply.")
    try:
        raw = llm.classify_json(SYSTEM, user)
    except Exception as e:  # never let the classifier break a reply
        print(f"[modes] classify failed ({e}); holding mode {current_mode}")
        raw = {"mode": current_mode}
    return normalize(raw, current_mode)


def mode_directive(mode: str) -> str:
    return {
        "COLD": "You are in COLD mode: brand new. Be warm and welcoming, learn their name. No flirting push.",
        "WARM": "You are in WARM mode: friendly, light flirting. Build rapport.",
        "FLIRTY": "You are in FLIRTY mode: suggestive and teasing is welcome. Explicit is NOT yet.",
        "EXPLICIT": "You are in EXPLICIT mode: the fan is sexual; explicit language and NSFW/PPV are allowed.",
    }.get(mode, "")
