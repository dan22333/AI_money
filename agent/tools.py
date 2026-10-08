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
from config import settings
from fanvue_client import fanvue

# per-request context the deterministic tools read
_ctx: contextvars.ContextVar[dict] = contextvars.ContextVar("req_ctx", default={})


def set_context(fan_id: str, user_uuid: str, dry_run: bool = False) -> dict:
    ctx = {"fan_id": fan_id, "user_uuid": user_uuid, "dry_run": dry_run,
           "mode": "COLD", "monetize_ok": False, "session_id": "", "outbox": []}
    _ctx.set(ctx)
    return ctx


def update_context(**kw) -> None:
    ctx = _ctx.get()
    ctx.update(kw)


def get_outbox() -> list:
    return _ctx.get().get("outbox", [])


def _send(text, media_uuids=None, price_cents=0):
    ctx = _ctx.get()
    if not ctx.get("dry_run") and fanvue.ready:
        fanvue.send_message(ctx["user_uuid"], text, media_uuids=media_uuids,
                            price_cents=price_cents or None)


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
def send_teaser(theme: str, caption: str) -> str:
    """Send a FREE teaser photo/video that fits `theme` (e.g. 'gym selfie', 'beach').
    You give the vibe + caption; the system picks an appropriate, not-already-seen item.
    Returns 'sent::<desc>' or 'nothing' if there's nothing suitable."""
    ctx = _ctx.get()
    items = store.eligible_media(ctx["fan_id"], ctx["mode"], theme, paid_only=False)
    items = [i for i in items if i.get("suggestedPriceCents", 0) == 0]
    if not items:
        return "nothing"
    item = items[0]
    ctx["outbox"].append({"type": "teaser", "mediaUuid": item["mediaUuid"], "caption": caption})
    _send(caption, media_uuids=[item["mediaUuid"]], price_cents=0)
    store.record_offer(ctx["fan_id"], item["mediaUuid"])
    return f"sent::{item['description']}"


@tool
def offer_content(theme: str, caption: str) -> str:
    """Attempt to sell paid (PPV) content that fits `theme`. You give the vibe + caption;
    the system deterministically checks timing, cooldown, and what's unsold, then either
    sends the best item at its price or declines.
    Returns: 'sent::<desc>::<price>' | 'nothing' (no unsold fit) | 'not_now' (bad timing)."""
    ctx = _ctx.get()
    fan_id, mode = ctx["fan_id"], ctx["mode"]
    # 1. timing gate
    if store.MODE_LEVEL.get(mode, 1) < store.MODE_LEVEL["FLIRTY"]:
        return "not_now"
    if not ctx.get("monetize_ok"):
        return "not_now"
    if store.cooldown_active(fan_id):
        return "not_now"
    # 2. eligible, unsold, appropriate paid items
    items = store.eligible_media(fan_id, mode, theme, paid_only=True)
    if not items:
        return "nothing"
    # 3-4. pick + price
    item = items[0]
    price = item.get("suggestedPriceCents") or settings.DEFAULT_PPV_PRICE_CENTS
    # 5. send
    ctx["outbox"].append({"type": "ppv", "mediaUuid": item["mediaUuid"],
                          "price_cents": price, "caption": caption})
    _send(caption, media_uuids=[item["mediaUuid"]], price_cents=price)
    # 6. record
    store.record_offer(fan_id, item["mediaUuid"])
    return f"sent::{item['description']}::${price/100:.2f}"


ALL_TOOLS = [send_message, save_fact, send_teaser, offer_content]
