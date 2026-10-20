"""Tools the agent can call.

The agent NEVER reasons over inventory, purchases, or prices. The selling/media
tools are deterministic: they run all checks and either act or decline. The agent
only supplies intent (which tool, with a theme) and Jenny's voice (the caption/text).
"""
from __future__ import annotations

import contextvars

from langchain_core.tools import tool

import memory
import store
from fanvue_client import fanvue

# per-request context the deterministic tools read
_ctx: contextvars.ContextVar[dict] = contextvars.ContextVar("req_ctx", default={})


def set_context(fan_id: str, user_uuid: str, dry_run: bool = False) -> dict:
    ctx = {"fan_id": fan_id, "user_uuid": user_uuid, "dry_run": dry_run,
           "mode": "COLD", "monetize_ok": False, "session_id": "", "outbox": [],
           "menu": {}}  # id -> sendable entry, built per-turn in graph.load_context
    _ctx.set(ctx)
    return ctx


def update_context(**kw) -> None:
    ctx = _ctx.get()
    ctx.update(kw)


def get_outbox() -> list:
    return _ctx.get().get("outbox", [])


def _send(text, media_uuids=None, price_cents=0):
    """Deliver a media message to Fanvue. Returns the send response dict on success
    ({} for sim/dry-run, which count as delivered), or None on a delivery FAILURE.

    Failures are NOT swallowed: we log the same `[send]` marker the alerts watch and
    return None so the calling tool reports 'send_failed' instead of pretending a
    photo went out. (Previously an exception here was eaten by the ToolNode and the
    plain text reply still sent — so the fan got words, no media, and we never knew.)"""
    ctx = _ctx.get()
    if ctx.get("dry_run") or not fanvue.ready:
        return {}
    try:
        return fanvue.send_message(ctx["user_uuid"], text, media_uuids=media_uuids,
                                   price_cents=price_cents or None)
    except Exception as e:
        print(f"[send] media delivery to Fanvue failed: {type(e).__name__}: {e}")
        return None


def _persist_sent_media(text, media_uuids, price_cents, resp) -> None:
    """Record the media message we just sent on the fan's transcript (so sends are
    auditable — previously mediaUuids/price were never stored). Keyed by the
    messageUuid Fanvue returns when present (idempotent vs. the reconcile re-pull),
    else a random-id append."""
    ctx = _ctx.get()
    fan_id = ctx["fan_id"]
    muid = ""
    if isinstance(resp, dict):
        m = resp.get("message") if isinstance(resp.get("message"), dict) else {}
        muid = str(resp.get("uuid") or resp.get("messageUuid") or m.get("uuid") or "")
    if muid:
        store.upsert_message(fan_id, muid, "jenny", text, media_uuids=media_uuids,
                             price_cents=price_cents, mode=ctx.get("mode", ""),
                             session_id=ctx.get("session_id", ""))
    else:
        store.save_message(fan_id, "jenny", text, media_uuids=media_uuids,
                           price_cents=price_cents, mode=ctx.get("mode", ""),
                           session_id=ctx.get("session_id", ""))


@tool
def send_message(text: str) -> str:
    """Send a plain text chat message to the fan as Jenny. Use for normal replies."""
    ctx = _ctx.get()
    ctx["outbox"].append({"type": "message", "text": text})
    _send(text)
    return "sent"


@tool
def save_fact(fact: str) -> str:
    """Remember a durable fact about this fan for next time.
    e.g. 'His name is Mike', 'Works night shifts', 'Has a dog named Rex'. Keep it short."""
    memory.add_fact(_ctx.get()["fan_id"], fact)
    return f"saved: {fact}"


@tool
def send_media(media_id: str, caption: str) -> str:
    """Send a photo/video from the "YOU CAN SEND NOW" menu in your context. Pass the
    menu id (e.g. 'm2') plus your caption. ONLY use ids that appear in the menu THIS
    turn — the menu is the single source of truth for what exists and what you may
    send right now. The system attaches the real file, sets the price (free teaser vs
    paid PPV) and delivers it — you never pick the file or the price yourself, just the
    id + caption. If the menu is empty you have nothing to send; don't claim you did.
    Returns 'sent::<desc>[::<price>]' | 'unavailable' (id not on the menu / selling not
    open yet) | 'send_failed' (delivery error)."""
    ctx = _ctx.get()
    entry = (ctx.get("menu") or {}).get((media_id or "").strip().lower())
    if not entry:
        return "unavailable"  # hallucinated / stale id — nothing sent
    media_uuids = entry["media_uuids"]
    price = int(entry.get("price_cents") or 0)
    paid = price > 0
    # Defensive re-check: the menu already gated paid items, but a SECOND paid send in
    # the same turn would bypass the cooldown the first one set — block it here.
    if paid and store.cooldown_active(ctx["fan_id"]):
        return "unavailable"
    resp = _send(caption, media_uuids=media_uuids, price_cents=price)
    if resp is None:
        return "send_failed"  # delivery error surfaced — don't record a phantom send
    ctx["outbox"].append({"type": entry["kind"], "mediaUuids": media_uuids,
                          "price_cents": price, "caption": caption})
    _persist_sent_media(caption, media_uuids, price, resp)
    # paid → burns the PPV cooldown + locks each item until paid; free teaser does neither
    store.record_offer(ctx["fan_id"], media_uuids, counts_toward_cooldown=paid)
    if paid:
        for u in media_uuids:
            store.add_pending_offer(ctx["fan_id"], u, price, caption)
        suffix = f"::{len(media_uuids)}" if len(media_uuids) > 1 else ""
        return f"sent::{entry['desc']}::${price/100:.2f}{suffix}"
    return f"sent::{entry['desc']}"


ALL_TOOLS = [send_message, save_fact, send_media]
