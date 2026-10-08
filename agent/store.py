"""Data stores: fans (users), messages, purchases, catalog.

Firestore on GCP (USE_FIRESTORE=true); in-memory fallback for local/dev/tests.
Simulated fans use a 'sim:' id prefix so test data never mixes with real analytics.

Mode→heat levels:  COLD=1  WARM=2  FLIRTY=3  EXPLICIT=4
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List

from config import settings

MODE_LEVEL = {"COLD": 1, "WARM": 2, "FLIRTY": 3, "EXPLICIT": 4}

# in-memory tables (fallback)
_fans: Dict[str, dict] = {}
_messages: Dict[str, List[dict]] = {}
_purchases: Dict[str, List[dict]] = {}
_catalog: Dict[str, dict] = {}

_db = None


def _now() -> datetime:  # overridable in tests
    return datetime.now(timezone.utc)


def reset_memory() -> None:
    """Clear in-memory tables (used by tests)."""
    _fans.clear(); _messages.clear(); _purchases.clear(); _catalog.clear()


# ---------------- fans (users table) ----------------
def get_fan(fan_id: str) -> dict:
    fan = _fans.get(fan_id)
    if fan is None:
        fan = {
            "fanUuid": fan_id, "firstSeenAt": _now().isoformat(), "lastSeenAt": None,
            "subscriptionStatus": "unknown", "totalSpendCents": 0, "purchaseCount": 0,
            "tier": "new", "currentMode": "COLD", "currentSessionId": None, "sessionCount": 0,
            "rollingSummary": "", "purchasedUuids": [], "recentlyOfferedUuids": [],
            "lastPpvAt": None, "offersThisSession": 0,
            "source": "sim" if fan_id.startswith("sim:") else "fanvue",
        }
        _fans[fan_id] = fan
    return fan


def save_fan(fan: dict) -> None:
    _fans[fan["fanUuid"]] = fan


def compute_session(fan_id: str) -> tuple[str, bool]:
    """Return (session_id, is_new). New session if gap since lastSeenAt exceeds threshold."""
    fan = get_fan(fan_id)
    now = _now()
    last = fan.get("lastSeenAt")
    is_new = True
    if last and fan.get("currentSessionId"):
        gap = now - datetime.fromisoformat(last)
        is_new = gap > timedelta(minutes=settings.SESSION_GAP_MINUTES)
    if is_new:
        sid = f"s{fan.get('sessionCount', 0) + 1}-{int(now.timestamp())}"
        fan["currentSessionId"] = sid
        fan["sessionCount"] = fan.get("sessionCount", 0) + 1
        fan["offersThisSession"] = 0  # reset cooldown counter per session
    # record activity so the NEXT message measures its gap from now (self-contained)
    fan["lastSeenAt"] = now.isoformat()
    save_fan(fan)
    return fan["currentSessionId"], is_new


# ---------------- messages ----------------
def save_message(fan_id: str, role: str, text: str, *, media_uuids=None,
                 price_cents: int = 0, mode: str = "", session_id: str = "") -> None:
    doc = {"fan_id": fan_id, "role": role, "text": text,
           "mediaUuids": media_uuids or [], "priceCents": price_cents,
           "mode": mode, "sessionId": session_id, "ts": _now().isoformat(),
           "source": "sim" if fan_id.startswith("sim:") else "fanvue"}
    _messages.setdefault(fan_id, []).append(doc)


def recent_messages(fan_id: str, limit: int = 15) -> List[dict]:
    return _messages.get(fan_id, [])[-limit:]


def message_count(fan_id: str) -> int:
    return len(_messages.get(fan_id, []))


# ---------------- purchases ----------------
def add_purchase(fan_id: str, invoice_id: str, *, gross_cents: int, source: str,
                 media_uuids=None, message_uuid: str = "") -> None:
    rows = _purchases.setdefault(fan_id, [])
    if any(r["invoiceId"] == invoice_id for r in rows):
        return  # idempotent
    rows.append({"invoiceId": invoice_id, "grossCents": gross_cents, "source": source,
                 "mediaUuids": media_uuids or [], "messageUuid": message_uuid,
                 "ts": _now().isoformat()})
    fan = get_fan(fan_id)
    fan["totalSpendCents"] = fan.get("totalSpendCents", 0) + gross_cents
    fan["purchaseCount"] = fan.get("purchaseCount", 0) + 1
    fan["lastPurchaseAt"] = _now().isoformat()
    for u in (media_uuids or []):
        if u not in fan["purchasedUuids"]:
            fan["purchasedUuids"].append(u)
    fan["tier"] = "whale" if fan["totalSpendCents"] >= 10000 else "buyer"
    save_fan(fan)


def purchased_uuids(fan_id: str) -> List[str]:
    return list(get_fan(fan_id).get("purchasedUuids", []))


# ---------------- catalog ----------------
def add_catalog_item(media_uuid: str, description: str, tags: List[str], heat: int,
                     suggested_price_cents: int = 0, meta_source: str = "human",
                     status: str = "ready") -> None:
    _catalog[media_uuid] = {"mediaUuid": media_uuid, "description": description,
                            "tags": tags, "heat": heat,
                            "suggestedPriceCents": suggested_price_cents,
                            "metaSource": meta_source, "status": status}


def eligible_media(fan_id: str, mode: str, theme: str = "", *, paid_only: bool = False) -> List[dict]:
    """Items this fan may be shown: appropriate heat, not already bought, not recently offered."""
    fan = get_fan(fan_id)
    level = MODE_LEVEL.get(mode, 1)
    bought = set(fan.get("purchasedUuids", []))
    offered = set(fan.get("recentlyOfferedUuids", []))
    q = (theme or "").lower().split()
    out = []
    for it in _catalog.values():
        if it.get("status") != "ready":
            continue
        if it["heat"] > level:
            continue
        if it["mediaUuid"] in bought or it["mediaUuid"] in offered:
            continue
        if paid_only and it.get("suggestedPriceCents", 0) <= 0:
            continue
        hay = (it["description"] + " " + " ".join(it["tags"])).lower()
        score = sum(1 for w in q if w in hay)
        out.append((score, it))
    out.sort(key=lambda x: (x[0], x[1]["heat"]), reverse=True)
    return [it for _, it in out]


# ---------------- cooldown / offers ----------------
def cooldown_active(fan_id: str) -> bool:
    fan = get_fan(fan_id)
    if fan.get("offersThisSession", 0) >= settings.MAX_OFFERS_PER_SESSION:
        return True
    last = fan.get("lastPpvAt")
    if last:
        if _now() - datetime.fromisoformat(last) < timedelta(minutes=settings.PPV_COOLDOWN_MINUTES):
            return True
    return False


def record_offer(fan_id: str, media_uuid: str) -> None:
    fan = get_fan(fan_id)
    fan.setdefault("recentlyOfferedUuids", []).append(media_uuid)
    fan["lastPpvAt"] = _now().isoformat()
    fan["offersThisSession"] = fan.get("offersThisSession", 0) + 1
    save_fan(fan)


# ---------------- demo seed ----------------
def seed_demo_catalog() -> None:
    if _catalog:
        return
    add_catalog_item("demo-beach-001", "sunrise beach run with dog Ace, sporty bikini",
                     ["beach", "dog", "sfw", "teaser"], heat=2, suggested_price_cents=0)
    add_catalog_item("demo-gym-002", "post-workout gym mirror selfie, playful smirk",
                     ["gym", "fitness", "selfie"], heat=3, suggested_price_cents=0)
    add_catalog_item("demo-ppv-003", "exclusive spicy late-night bedroom set",
                     ["intimate", "spicy", "exclusive", "nsfw", "ppv"], heat=4,
                     suggested_price_cents=1500)
    add_catalog_item("demo-ppv-004", "flirty lingerie tease set",
                     ["lingerie", "flirty", "tease", "ppv"], heat=3,
                     suggested_price_cents=800)
