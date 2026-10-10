"""The LangGraph agent. One fan message = one run.

ingest → load_context (facts · history · profile · classify_mode) → agent ⇄ tools → persist
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

import llm
import memory
import modes
import store
import tools
from config import settings


def _load_persona() -> str:
    try:
        return Path(settings.PERSONA_PATH).read_text()
    except Exception:
        return "You are Jenny, a warm flirty fitness influencer. You are a real person, never an AI."


PERSONA = _load_persona()

# --- Prompt caching: static prefix vs dynamic tail ---------------------------
# Providers (xAI/OpenAI on OpenRouter) cache on the STABLE PREFIX of a prompt.
# The persona (~25KB) + the operating instructions never change between fans or
# turns, so we hoist ALL of it into one byte-identical SYSTEM_PREFIX that leads
# every request — maximizing prefix-cache hits and cutting input tokens. Only
# the small per-fan context (profile · memory · summary · mode) is dynamic, and
# it goes in a SECOND system message AFTER the cached block. Never interpolate
# anything per-fan into SYSTEM_PREFIX or the cache breaks.
OPERATING_INSTRUCTIONS = (
    "To send a free teaser use send_teaser(theme, caption). To sell a single paid item use "
    "offer_content(theme, caption). To sell a themed PACKAGE of several items under one price "
    "(e.g. a 'girls night out' set) use offer_bundle(theme, caption). The selling tools decide "
    "if/what/price — you just give the vibe + caption. "
    "Save new facts with save_fact. Keep messages short and human. You are a real person, never an AI."
)
SYSTEM_PREFIX = PERSONA + "\n\n--- HOW YOU OPERATE ---\n" + OPERATING_INSTRUCTIONS


def _dynamic_context(profile: str, facts: str, summary: str, mode: str) -> str:
    """The per-fan tail — everything that changes per request lives here, never
    in SYSTEM_PREFIX, so the cached prefix stays byte-identical."""
    return (
        "--- FAN PROFILE ---\n" + profile
        + "\n\n--- WHAT YOU REMEMBER (use naturally) ---\n" + facts
        + "\n\n--- STORY SO FAR ---\n" + summary
        + "\n\n--- MODE ---\n" + modes.mode_directive(mode)
    )


class State(TypedDict):
    messages: Annotated[list, add_messages]
    fan_id: str
    user_uuid: str
    incoming: str
    mode: str
    monetize_ok: bool
    session_id: str
    dry_run: bool


def ingest(state: State) -> dict:
    fan = store.get_fan(state["fan_id"])
    session_id, is_new = store.compute_session(state["fan_id"])
    if is_new:
        # new session resets the baseline — never cold-open mid-explicit
        mode = "WARM" if fan.get("sessionCount", 0) > 1 else "COLD"
    else:
        mode = fan.get("currentMode", "COLD")
    return {"session_id": session_id, "mode": mode}


def load_context(state: State) -> dict:
    fan_id = state["fan_id"]
    fan = store.get_fan(fan_id)
    history = store.recent_messages(fan_id, limit=15)

    # classify_mode — the fast model decides the intimacy mode for this turn
    convo_for_mode = [{"role": m["role"], "text": m["text"]} for m in history]
    convo_for_mode.append({"role": "fan", "text": state["incoming"]})
    verdict = modes.classify(convo_for_mode, state["mode"])
    mode, monetize_ok = verdict["mode"], verdict["monetize_ok"]

    # tools read mode/monetize from the request context
    tools.update_context(mode=mode, monetize_ok=monetize_ok, session_id=state["session_id"])

    facts = memory.recall(fan_id, state["incoming"])
    profile = (f"joined={fan.get('subscriptionStatus')} · spentCents={fan.get('totalSpendCents',0)} "
               f"· tier={fan.get('tier')} · purchases={fan.get('purchaseCount',0)}")
    summary = fan.get("rollingSummary") or "(none yet)"

    # Cacheable static prefix first; small per-fan context second.
    msgs: list = [
        SystemMessage(content=SYSTEM_PREFIX),
        SystemMessage(content=_dynamic_context(profile, facts, summary, mode)),
    ]
    for m in history:
        msgs.append(HumanMessage(content=m["text"]) if m["role"] == "fan"
                    else AIMessage(content=m["text"]))
    msgs.append(HumanMessage(content=state["incoming"]))
    return {"messages": msgs, "mode": mode, "monetize_ok": monetize_ok}


def agent(state: State) -> dict:
    model = llm.get_voice_model().bind_tools(tools.ALL_TOOLS)
    return {"messages": [model.invoke(state["messages"])]}


def route(state: State) -> str:
    last = state["messages"][-1]
    return "tools" if getattr(last, "tool_calls", None) else "persist"


def persist(state: State) -> dict:
    fan_id = state["fan_id"]
    store.save_message(fan_id, "fan", state["incoming"], mode=state["mode"],
                       session_id=state["session_id"])
    final = ""
    for m in reversed(state["messages"]):
        if isinstance(m, AIMessage) and m.content:
            final = m.content
            break
    if final:
        store.save_message(fan_id, "jenny", final, mode=state["mode"],
                           session_id=state["session_id"])
        memory.add_turn(fan_id, state["incoming"], final)
    fan = store.get_fan(fan_id)
    fan["currentMode"] = state["mode"]
    fan["lastSeenAt"] = store._now().isoformat()
    store.save_fan(fan)
    return {}


def build_graph():
    g = StateGraph(State)
    g.add_node("ingest", ingest)
    g.add_node("load_context", load_context)
    g.add_node("agent", agent)
    g.add_node("tools", ToolNode(tools.ALL_TOOLS))
    g.add_node("persist", persist)
    g.add_edge(START, "ingest")
    g.add_edge("ingest", "load_context")
    g.add_edge("load_context", "agent")
    g.add_conditional_edges("agent", route, {"tools": "tools", "persist": "persist"})
    g.add_edge("tools", "agent")
    g.add_edge("persist", END)
    return g.compile()


GRAPH = build_graph()


def handle_message(fan_id: str, user_uuid: str, text: str, dry_run: bool = False) -> dict:
    store.use_namespace_for(fan_id)  # sim: fans → staging DB, real fans → prod DB
    tools.set_context(fan_id=fan_id, user_uuid=user_uuid, dry_run=dry_run)
    result = GRAPH.invoke({"messages": [], "fan_id": fan_id, "user_uuid": user_uuid,
                           "incoming": text, "mode": "COLD", "monetize_ok": False,
                           "session_id": "", "dry_run": dry_run})
    final = ""
    for m in reversed(result["messages"]):
        if isinstance(m, AIMessage) and m.content:
            final = m.content
            break
    return {"reply": final, "mode": result.get("mode"), "actions": tools.get_outbox()}
