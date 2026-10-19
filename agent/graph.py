"""The LangGraph agent. One fan message = one run.

ingest → load_context (facts · history · profile · classify_mode) → agent ⇄ tools → persist
"""
from __future__ import annotations

import contextvars
from concurrent.futures import ThreadPoolExecutor
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
import tasks
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
    through_ts: str    # batch mode: answer stored history up to (and incl.) this ts;
                       # "" == normal single-message mode (incoming is the new turn)
    system_note: bool  # incoming is a synthetic internal directive (e.g. a purchase
                       # reaction), NOT a real fan utterance → stored kind="system"
                       # so the debounce worker never tries to "answer" it
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
    through_ts = state.get("through_ts") or ""
    fan = store.get_fan(fan_id)
    history = store.recent_messages(fan_id, limit=15)
    # Batch mode: the unanswered fan messages are ALREADY the tail of history, so
    # we answer history as-is. Cap it at the batch boundary so a message that
    # landed AFTER we fixed the batch (Scenario C) is excluded here and handled by
    # its own task — keeps the reply deterministic w.r.t. the captured batch.
    if through_ts:
        history = [m for m in history if (m.get("ts") or "") <= through_ts]

    convo_for_mode = [{"role": m["role"], "text": m["text"]} for m in history]
    if state["incoming"]:  # normal mode: the new turn isn't in history yet
        convo_for_mode.append({"role": "fan", "text": state["incoming"]})
    # recall input: the latest fan text we're answering (incoming turn, or the last
    # fan message in the batch) + the last few turns for context. The strategy
    # (latest | context | expanded) decides how that becomes a search query.
    incoming = state["incoming"] or next(
        (m["text"] for m in reversed(history) if m.get("role") == "fan"), "")
    recent_turns = [{"role": m["role"], "text": m["text"]}
                    for m in history[-settings.RECALL_CONTEXT_TURNS:]]

    # classify_mode (fast mode LLM) and recall (embed + pgvector, maybe +expansion)
    # are independent and both block on the network, so run them concurrently — saves
    # the smaller off the critical path. copy_context() carries the Firestore/mem0
    # namespace (a ContextVar) into each worker thread, which plain threads would NOT
    # inherit (→ wrong DB for sim fans).
    ctx_mode, ctx_recall = contextvars.copy_context(), contextvars.copy_context()
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_mode = ex.submit(ctx_mode.run, modes.classify, convo_for_mode, state["mode"])
        f_facts = ex.submit(ctx_recall.run, memory.recall_contextual, fan_id, incoming,
                            recent_turns, strategy=settings.RECALL_STRATEGY)
        verdict = f_mode.result()
        facts = f_facts.result()
    mode, monetize_ok = verdict["mode"], verdict["monetize_ok"]

    # tools read mode/monetize from the request context
    tools.update_context(mode=mode, monetize_ok=monetize_ok, session_id=state["session_id"])
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
    if state["incoming"]:  # batch mode leaves the tail of history as the latest turn
        msgs.append(HumanMessage(content=state["incoming"]))
    return {"messages": msgs, "mode": mode, "monetize_ok": monetize_ok}


def agent(state: State) -> dict:
    model = llm.get_voice_model().bind_tools(tools.ALL_TOOLS)
    try:
        return {"messages": [model.invoke(state["messages"])]}
    except Exception as e:
        # voice model = Grok via OpenRouter; log the same marker the other LLM paths
        # use so the OpenRouter alert catches a provider outage on the reply path too.
        print(f"[llm] openrouter error model=voice ({type(e).__name__})")
        raise


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
    inputs that carry no uuid.

    Batch mode (through_ts set): the fan messages were already written by the
    webhook (store.record_inbound) before the worker ran, so we DON'T write them
    here — we just refresh the fan's live state."""
    fan_id = state["fan_id"]
    uuid = state.get("fan_msg_uuid") or ""
    kind = "system" if state.get("system_note") else "fan"
    if not state.get("through_ts"):  # normal single-message mode
        if uuid:
            store.upsert_message(fan_id, uuid, "fan", state["incoming"], kind=kind,
                                 mode=state["mode"], session_id=state["session_id"])
        else:
            store.save_message(fan_id, "fan", state["incoming"], kind=kind,
                               mode=state["mode"], session_id=state["session_id"])
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


def _record_reply(fan_id: str, final: str, *, mode: str, session_id: str,
                  jenny_uuid: str) -> None:
    """Store Jenny's reply doc, keyed by the messageUuid the send API returned when
    we have it (idempotent vs. the reconcile re-pull), else a random-id append."""
    if jenny_uuid:
        store.upsert_message(fan_id, jenny_uuid, "jenny", final,
                             mode=mode, session_id=session_id)
    else:
        store.save_message(fan_id, "jenny", final, mode=mode, session_id=session_id)


def learn_from_turn(fan_id: str, incoming: str, final: str) -> None:
    """Post-reply bookkeeping: extract durable facts into mem0 (embed + extraction
    LLM, ~1-3s) and refresh the rolling summary. Safe to run AFTER the per-fan lease
    is released — it never advances the watermark, writes only to the memory store
    (pgvector) and the summary fields (via an ATOMIC merge, so it can't clobber a
    lease a concurrent worker just took), and is best-effort (a failure just means
    this turn isn't summarised; the facts are re-extractable by reconcile)."""
    memory.add_turn(fan_id, incoming, final)
    fan = store.get_fan(fan_id)
    if summary.maybe_update(fan, store.recent_messages(fan_id, limit=settings.SUMMARIZE_AFTER),
                            store.message_count(fan_id)):
        store.merge_fan_fields(fan_id, {"rollingSummary": fan.get("rollingSummary", ""),
                                        "summarizedAtCount": fan.get("summarizedAtCount", 0)})


def maybe_learn(fan_id: str, incoming: str, final: str) -> None:
    """Run the post-reply learning, or ENQUEUE it when fan-out is on. With fan-out,
    mem0 extraction + summary happen in a separate Cloud Tasks worker
    (/admin/extract_memory) so they never block the reply path; otherwise inline."""
    if tasks.enqueue_extract_memory(fan_id, incoming, final):
        return
    learn_from_turn(fan_id, incoming, final)


def _persist_reply(fan_id: str, incoming: str, final: str, *, mode: str,
                   session_id: str, jenny_uuid: str) -> None:
    """Synchronous path: store the reply then learn (inline or fan-out)."""
    _record_reply(fan_id, final, mode=mode, session_id=session_id, jenny_uuid=jenny_uuid)
    maybe_learn(fan_id, incoming, final)


def _final_text(result: dict) -> str:
    for m in reversed(result["messages"]):
        if isinstance(m, AIMessage) and m.content:
            return m.content
    return ""


def _deliver_and_record(fan_id: str, user_uuid: str, final: str, actions: list, *,
                        mode: str, session_id: str, memory_incoming: str,
                        dry_run: bool, strict_send: bool = False,
                        learn: bool = True) -> dict:
    """Send the reply to Fanvue (send-FIRST), then record it in our store + memory.
    This ordering is deliberate: our DB never claims a reply the fan didn't get —
    a crash can only leave 'sent but not yet recorded' (recoverable), never
    'recorded but never sent' (a silent ghost).

    Returns {sent, attempted, jenny_uuid}. The media/selling tools already POST
    their own messages, so a plain conversational reply is deduped against those.

    strict_send (debounce worker): on a delivery EXCEPTION, do NOT record the reply
    and re-raise — the caller must not advance the watermark for an unsent reply, so
    the task retries. Non-strict (synchronous webhook): preserve the old behavior —
    swallow the error, record anyway, and return (the webhook still 200s)."""
    if not (isinstance(final, str) and final.strip()):
        return {"sent": False, "attempted": False, "jenny_uuid": ""}
    jenny_uuid = ""
    attempted = sent = False
    if not dry_run:
        from fanvue_client import fanvue
        already_sent = any(a.get("type") == "message" and a.get("text") == final
                           for a in actions)
        if fanvue.ready and not already_sent:
            attempted = True
            try:
                resp = fanvue.send_message(user_uuid, final)
                jenny_uuid = _reply_uuid(resp)
                sent = True
                actions.append({"type": "message", "text": final, "auto": True})
            except Exception as e:
                print(f"[send] delivery to Fanvue failed: {type(e).__name__}: {e}")
                if strict_send:
                    raise  # don't record; let the worker retry without advancing
    if learn:
        # synchronous path: store the reply AND learn in one shot
        _persist_reply(fan_id, memory_incoming, final, mode=mode, session_id=session_id,
                       jenny_uuid=jenny_uuid)
    else:
        # debounce worker: store the reply NOW (must be durable before the watermark
        # advances), but defer learn_from_turn until the caller has released the lease
        _record_reply(fan_id, final, mode=mode, session_id=session_id, jenny_uuid=jenny_uuid)
    return {"sent": sent, "attempted": attempted, "jenny_uuid": jenny_uuid}


def handle_message(fan_id: str, user_uuid: str, text: str, *,
                   fan_msg_uuid: str = "", dry_run: bool = False,
                   system_note: bool = False) -> dict:
    store.use_namespace_for(fan_id)  # sim: fans → staging DB, real fans → prod DB
    tools.set_context(fan_id=fan_id, user_uuid=user_uuid, dry_run=dry_run)
    result = GRAPH.invoke({"messages": [], "fan_id": fan_id, "user_uuid": user_uuid,
                           "incoming": text, "fan_msg_uuid": fan_msg_uuid,
                           "through_ts": "", "system_note": system_note,
                           "mode": "COLD", "monetize_ok": False,
                           "session_id": "", "dry_run": dry_run})
    final = _final_text(result)
    actions = tools.get_outbox()
    _deliver_and_record(fan_id, user_uuid, final, actions,
                        mode=result.get("mode") or "COLD",
                        session_id=result.get("session_id") or "",
                        memory_incoming=text, dry_run=dry_run)
    return {"reply": final, "mode": result.get("mode"), "actions": actions}


def handle_batch(fan_id: str, user_uuid: str, through_ts: str, batch_text: str) -> dict:
    """Debounce worker generation: answer the fan's unanswered batch (already stored
    in history, up to `through_ts`) with ONE reply, then send + record it. Always
    real delivery (debounce only runs in prod). `batch_text` is the concatenation
    of the batch messages, used only to key the memory turn."""
    store.use_namespace_for(fan_id)
    tools.set_context(fan_id=fan_id, user_uuid=user_uuid, dry_run=False)
    result = GRAPH.invoke({"messages": [], "fan_id": fan_id, "user_uuid": user_uuid,
                           "incoming": "", "fan_msg_uuid": "", "through_ts": through_ts,
                           "system_note": False, "mode": "COLD", "monetize_ok": False,
                           "session_id": "", "dry_run": False})
    final = _final_text(result)
    actions = tools.get_outbox()
    # learn=False: the reply is stored + sent here, but mem0 extraction + summary
    # are deferred to the worker (after it releases the lease) via learn_from_turn.
    delivery = _deliver_and_record(fan_id, user_uuid, final, actions,
                                   mode=result.get("mode") or "COLD",
                                   session_id=result.get("session_id") or "",
                                   memory_incoming=batch_text, dry_run=False,
                                   strict_send=True, learn=False)
    return {"reply": final, "mode": result.get("mode"), "actions": actions,
            "sent": delivery["sent"], "attempted": delivery["attempted"]}


def _reply_uuid(resp) -> str:
    """Pull the created message's uuid out of the Fanvue send response. Shape isn't
    contractually pinned, so check the common spots; "" if absent (→ random-id write)."""
    if not isinstance(resp, dict):
        return ""
    msg = resp.get("message") if isinstance(resp.get("message"), dict) else {}
    return str(resp.get("uuid") or resp.get("messageUuid") or msg.get("uuid") or "")
