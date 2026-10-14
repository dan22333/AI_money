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
import summary
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
    fan_msg_uuid: str  # Fanvue messageUuid of the inbound fan message ("" if unknown)
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
    """Store the FAN's inbound message + update the fan's live state. Jenny's reply
    is persisted later in handle_message (after send), keyed by the uuid the send
    API returns — see _persist_reply. We DON'T write the reply here because its
    messageUuid isn't known until it's sent.

    The fan message is keyed by its Fanvue messageUuid when we have it (idempotent:
    the nightly reconcile re-pulls the same message and collides instead of writing
    a second random-id copy). Falls back to a random-id append for sim/synthetic
    inputs that carry no uuid."""
    fan_id = state["fan_id"]
    uuid = state.get("fan_msg_uuid") or ""
    if uuid:
        store.upsert_message(fan_id, uuid, "fan", state["incoming"],
                             mode=state["mode"], session_id=state["session_id"])
    else:
        store.save_message(fan_id, "fan", state["incoming"], mode=state["mode"],
                           session_id=state["session_id"])
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


def _persist_reply(fan_id: str, incoming: str, final: str, *, mode: str,
                   session_id: str, jenny_uuid: str) -> None:
    """Store Jenny's reply once the turn is complete, keyed by the messageUuid the
    send API returned when we have it (idempotent vs. the reconcile re-pull), else
    a random-id append (sim / dry-run / send not attempted). Then record the turn
    in mem0 and refresh the rolling summary now that both sides are stored."""
    if jenny_uuid:
        store.upsert_message(fan_id, jenny_uuid, "jenny", final,
                             mode=mode, session_id=session_id)
    else:
        store.save_message(fan_id, "jenny", final, mode=mode, session_id=session_id)
    memory.add_turn(fan_id, incoming, final)
    fan = store.get_fan(fan_id)
    summary.maybe_update(fan, store.recent_messages(fan_id, limit=settings.SUMMARIZE_AFTER),
                         store.message_count(fan_id))
    store.save_fan(fan)


def handle_message(fan_id: str, user_uuid: str, text: str, *,
                   fan_msg_uuid: str = "", dry_run: bool = False) -> dict:
    store.use_namespace_for(fan_id)  # sim: fans → staging DB, real fans → prod DB
    tools.set_context(fan_id=fan_id, user_uuid=user_uuid, dry_run=dry_run)
    result = GRAPH.invoke({"messages": [], "fan_id": fan_id, "user_uuid": user_uuid,
                           "incoming": text, "fan_msg_uuid": fan_msg_uuid,
                           "mode": "COLD", "monetize_ok": False,
                           "session_id": "", "dry_run": dry_run})
    final = ""
    for m in reversed(result["messages"]):
        if isinstance(m, AIMessage) and m.content:
            final = m.content
            break
    actions = tools.get_outbox()
    mode = result.get("mode") or "COLD"
    session_id = result.get("session_id") or ""
    # Deliver the reply. The media/selling tools (send_teaser/offer_*) already
    # POST their own messages via tools._send; a plain conversational reply is
    # just AIMessage text and nothing sent it, so the harness delivers it here.
    # Gated by dry_run (so /simulate + the canary never message a real fan) and
    # deduped against a message a tool already sent with the same text. We capture
    # the uuid the send returns so the reply is stored idempotently (see #3).
    jenny_uuid = ""
    if isinstance(final, str) and final.strip() and not dry_run:
        from fanvue_client import fanvue
        already_sent = any(a.get("type") == "message" and a.get("text") == final
                           for a in actions)
        if fanvue.ready and not already_sent:
            try:
                resp = fanvue.send_message(user_uuid, final)
                jenny_uuid = _reply_uuid(resp)
                actions.append({"type": "message", "text": final, "auto": True})
            except Exception as e:  # never let a delivery error crash the handler
                print(f"[send] delivery to Fanvue failed: {type(e).__name__}: {e}")
    if isinstance(final, str) and final.strip():
        _persist_reply(fan_id, text, final, mode=mode, session_id=session_id,
                       jenny_uuid=jenny_uuid)
    return {"reply": final, "mode": result.get("mode"), "actions": actions}


def _reply_uuid(resp) -> str:
    """Pull the created message's uuid out of the Fanvue send response. Shape isn't
    contractually pinned, so check the common spots; "" if absent (→ random-id write)."""
    if not isinstance(resp, dict):
        return ""
    msg = resp.get("message") if isinstance(resp.get("message"), dict) else {}
    return str(resp.get("uuid") or resp.get("messageUuid") or msg.get("uuid") or "")
