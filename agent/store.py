"""Data stores: fans (users), messages, purchases, catalog, bundles.

Two interchangeable backends behind one small interface:
  - MemoryBackend   — plain dicts; used for local dev, tests, and CI (USE_FIRESTORE=false).
  - FirestoreBackend — Google Cloud Firestore; used on GCP (USE_FIRESTORE=true).
The SAME logic (tiers, eligibility, cooldown, bundle pricing) runs on both — only
the persistence primitives differ. This is what lets the emulator test the real
Firestore code path without the cloud.

Collections:
  fans/{fanId}                      — the user row
  fans/{fanId}/messages/{autoId}    — chat transcript (ordered by ts)
  fans/{fanId}/purchases/{invoice}  — purchases (doc id = invoice → idempotent)
  catalog/{mediaUuid}               — individual sellable/teaser media
  bundles/{bundleId}                — a named set of media sold under one price

Simulated fans use a 'sim:' id prefix so test data never mixes with real analytics.
Mode→heat levels:  COLD=1  WARM=2  FLIRTY=3  EXPLICIT=4
"""
from __future__ import annotations

import contextvars
import copy
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from config import settings

MODE_LEVEL = {"COLD": 1, "WARM": 2, "FLIRTY": 3, "EXPLICIT": 4}

# Which named Firestore database a request's writes/reads target. Synthetic
# `sim:` fans (/simulate, canary smoke test) → "sim" (staging DB); everyone
# else → "real" (default DB). The MemoryBackend ignores this entirely.
_namespace: contextvars.ContextVar[str] = contextvars.ContextVar("fs_namespace", default="real")


def use_namespace_for(fan_id: str) -> None:
    """Route this request to the staging DB for sim fans, prod DB otherwise."""
    _namespace.set("sim" if str(fan_id).startswith("sim:") else "real")


def current_namespace() -> str:
    return _namespace.get()

FANS = "fans"
CATALOG = "catalog"
BUNDLES = "bundles"
MESSAGES = "messages"
PURCHASES = "purchases"
PROCESSED = "processed_events"  # webhook idempotency ledger


def _now() -> datetime:  # overridable in tests
    return datetime.now(timezone.utc)


def _docid(raw: str) -> str:
    """Firestore doc ids cannot contain '/'. Sanitize defensively."""
    return raw.replace("/", "_")


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------
class MemoryBackend:
    """In-memory document store. Insertion order == chronological order."""

    def __init__(self) -> None:
        self._docs: Dict[str, Dict[str, dict]] = {}
        self._subs: Dict[tuple, Dict[str, dict]] = {}
        self._counter = 0

    # top-level docs
    def get(self, col: str, doc_id: str) -> Optional[dict]:
        d = self._docs.get(col, {}).get(_docid(doc_id))
        return copy.deepcopy(d) if d is not None else None

    def set(self, col: str, doc_id: str, data: dict) -> None:
        self._docs.setdefault(col, {})[_docid(doc_id)] = copy.deepcopy(data)

    def create_if_absent(self, col: str, doc_id: str, data: dict) -> bool:
        bucket = self._docs.setdefault(col, {})
        key = _docid(doc_id)
        if key in bucket:
            return False
        bucket[key] = copy.deepcopy(data)
        return True

    def list(self, col: str) -> List[dict]:
        return [copy.deepcopy(d) for d in self._docs.get(col, {}).values()]

    # subcollections
    def _skey(self, col, doc_id, sub):
        return (col, _docid(doc_id), sub)

    def sub_add(self, col, doc_id, sub, data) -> None:
        mid = f"m{self._counter}"
        self._counter += 1
        self._subs.setdefault(self._skey(col, doc_id, sub), {})[mid] = copy.deepcopy(data)

    def sub_set(self, col, doc_id, sub, sub_id, data) -> None:
        self._subs.setdefault(self._skey(col, doc_id, sub), {})[_docid(sub_id)] = copy.deepcopy(data)

    def sub_get(self, col, doc_id, sub, sub_id) -> Optional[dict]:
        d = self._subs.get(self._skey(col, doc_id, sub), {}).get(_docid(sub_id))
        return copy.deepcopy(d) if d is not None else None

    def sub_all(self, col, doc_id, sub) -> List[dict]:
        return [copy.deepcopy(d) for d in self._subs.get(self._skey(col, doc_id, sub), {}).values()]

    def sub_recent(self, col, doc_id, sub, order_field, limit) -> List[dict]:
        vals = list(self._subs.get(self._skey(col, doc_id, sub), {}).values())
        return [copy.deepcopy(d) for d in vals[-limit:]]

    def sub_count(self, col, doc_id, sub) -> int:
        return len(self._subs.get(self._skey(col, doc_id, sub), {}))

    def clear(self) -> None:
        self._docs.clear()
        self._subs.clear()
        self._counter = 0


class FirestoreBackend:
    """Google Cloud Firestore. Auto-targets the emulator if FIRESTORE_EMULATOR_HOST is set."""

    def __init__(self) -> None:
        from google.cloud import firestore
        self._fs = firestore
        # one client per named database; routed per-request by current_namespace()
        self._clients = {
            "real": firestore.Client(project=settings.GCP_PROJECT,
                                     database=settings.FIRESTORE_DATABASE),
            "sim": firestore.Client(project=settings.GCP_PROJECT,
                                    database=settings.SIM_FIRESTORE_DATABASE),
        }

    @property
    def db(self):
        return self._clients[current_namespace()]

    def get(self, col, doc_id):
        snap = self.db.collection(col).document(_docid(doc_id)).get()
        return snap.to_dict() if snap.exists else None

    def set(self, col, doc_id, data):
        self.db.collection(col).document(_docid(doc_id)).set(data)

    def create_if_absent(self, col, doc_id, data) -> bool:
        """Atomic create: True if we created it, False if it already existed.
        Firestore's create() is a transaction (fails if the doc exists), so this
        is safe across concurrent Cloud Run instances."""
        from google.api_core.exceptions import AlreadyExists
        try:
            self.db.collection(col).document(_docid(doc_id)).create(data)
            return True
        except AlreadyExists:
            return False

    def list(self, col):
        return [s.to_dict() for s in self.db.collection(col).stream()]

    def _subref(self, col, doc_id, sub):
        return self.db.collection(col).document(_docid(doc_id)).collection(sub)

    def sub_add(self, col, doc_id, sub, data):
        self._subref(col, doc_id, sub).add(data)

    def sub_set(self, col, doc_id, sub, sub_id, data):
        self._subref(col, doc_id, sub).document(_docid(sub_id)).set(data)

    def sub_get(self, col, doc_id, sub, sub_id):
        snap = self._subref(col, doc_id, sub).document(_docid(sub_id)).get()
        return snap.to_dict() if snap.exists else None

    def sub_all(self, col, doc_id, sub):
        return [s.to_dict() for s in self._subref(col, doc_id, sub).stream()]

    def sub_recent(self, col, doc_id, sub, order_field, limit):
        q = (self._subref(col, doc_id, sub)
             .order_by(order_field, direction=self._fs.Query.DESCENDING)
             .limit(limit))
        rows = [s.to_dict() for s in q.stream()]
        rows.reverse()  # newest-first query → return chronological
        return rows

    def sub_count(self, col, doc_id, sub):
        return sum(1 for _ in self._subref(col, doc_id, sub).stream())

    def clear(self) -> None:
        """Delete all docs in the known collections (+ their subcollections).
        Used only by tests against the emulator — never run this on real data.

        HARD GUARD: refuse to run unless we're talking to the emulator. Without
        FIRESTORE_EMULATOR_HOST this is a live cloud database and clear() would
        wipe production. This makes an accidental prod wipe impossible."""
        import os
        if not os.environ.get("FIRESTORE_EMULATOR_HOST"):
            raise RuntimeError(
                "refusing to clear(): FIRESTORE_EMULATOR_HOST is not set, so this "
                "would delete a REAL Firestore database. clear() is emulator-only.")
        for db in {id(c): c for c in self._clients.values()}.values():  # unique clients
            for col in (FANS,):
                for doc in db.collection(col).list_documents():
                    for sub in (MESSAGES, PURCHASES):
                        for s in doc.collection(sub).list_documents():
                            s.delete()
                    doc.delete()
            for col in (CATALOG, BUNDLES, PROCESSED):
                for doc in db.collection(col).list_documents():
                    doc.delete()


_backend_instance = None


def _backend():
    global _backend_instance
    if _backend_instance is None:
        _backend_instance = FirestoreBackend() if settings.USE_FIRESTORE else MemoryBackend()
    return _backend_instance


def reset_memory() -> None:
    """Clear all tables (used by tests). Works for both backends."""
    _backend().clear()


# ---------------- fans (users table) ----------------
def _default_fan(fan_id: str) -> dict:
    return {
        "fanUuid": fan_id, "firstSeenAt": _now().isoformat(), "lastSeenAt": None,
        "subscriptionStatus": "unknown", "totalSpendCents": 0, "purchaseCount": 0,
        "tier": "new", "currentMode": "COLD", "currentSessionId": None, "sessionCount": 0,
        "rollingSummary": "", "purchasedUuids": [], "recentlyOfferedUuids": [],
        "lastPpvAt": None, "offersThisSession": 0,
        "source": "sim" if fan_id.startswith("sim:") else "fanvue",
    }


def get_fan(fan_id: str) -> dict:
    fan = _backend().get(FANS, fan_id)
    if fan is None:
        fan = _default_fan(fan_id)
        _backend().set(FANS, fan_id, fan)
    return fan


def save_fan(fan: dict) -> None:
    _backend().set(FANS, fan["fanUuid"], fan)


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
    _backend().sub_add(FANS, fan_id, MESSAGES, doc)


def recent_messages(fan_id: str, limit: int = 15) -> List[dict]:
    return _backend().sub_recent(FANS, fan_id, MESSAGES, "ts", limit)


def message_count(fan_id: str) -> int:
    return _backend().sub_count(FANS, fan_id, MESSAGES)


# ---------------- purchases ----------------
def add_purchase(fan_id: str, invoice_id: str, *, gross_cents: int, source: str,
                 media_uuids=None, message_uuid: str = "") -> None:
    if _backend().sub_get(FANS, fan_id, PURCHASES, invoice_id) is not None:
        return  # idempotent (doc id = invoice)
    _backend().sub_set(FANS, fan_id, PURCHASES, invoice_id,
                       {"invoiceId": invoice_id, "grossCents": gross_cents, "source": source,
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


# ---------------- webhook idempotency ----------------
def claim_event(event_id: str) -> bool:
    """Atomically claim a webhook event id. Returns True if this is the FIRST
    time we've seen it (caller should process), False if already processed.

    Backed by Firestore's atomic create() in prod, so it's durable across
    restarts and correct across concurrent Cloud Run instances — unlike the old
    in-process set which reset on every cold start and wasn't shared."""
    return _backend().create_if_absent(PROCESSED, event_id, {"ts": _now().isoformat()})


# ---------------- catalog ----------------
def add_catalog_item(media_uuid: str, description: str, tags: List[str], heat: int,
                     suggested_price_cents: int = 0, meta_source: str = "human",
                     status: str = "ready") -> None:
    _backend().set(CATALOG, media_uuid, {"mediaUuid": media_uuid, "description": description,
                                         "tags": tags, "heat": heat,
                                         "suggestedPriceCents": suggested_price_cents,
                                         "metaSource": meta_source, "status": status})


def _catalog_item(media_uuid: str) -> dict:
    return _backend().get(CATALOG, media_uuid) or {}


def eligible_media(fan_id: str, mode: str, theme: str = "", *, paid_only: bool = False) -> List[dict]:
    """Items this fan may be shown: appropriate heat, not already bought, not recently offered."""
    fan = get_fan(fan_id)
    level = MODE_LEVEL.get(mode, 1)
    bought = set(fan.get("purchasedUuids", []))
    offered = set(fan.get("recentlyOfferedUuids", []))
    q = (theme or "").lower().split()
    out = []
    for it in _backend().list(CATALOG):
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


# ---------------- bundles ----------------
def add_bundle(bundle_id: str, title: str, description: str, tags: List[str], heat: int,
               items: List[str], bundle_price_cents: int, meta_source: str = "human",
               status: str = "ready") -> None:
    """A bundle groups several catalog media under one package price (e.g. 'Girls Night Out').
    Members may also be sold individually; buying the bundle unlocks all of them."""
    _backend().set(BUNDLES, bundle_id, {"bundleId": bundle_id, "title": title,
                                        "description": description, "tags": tags, "heat": heat,
                                        "items": items, "bundlePriceCents": bundle_price_cents,
                                        "metaSource": meta_source, "status": status})


def _value_of(uuids: List[str]) -> tuple[int, bool]:
    """Sum of standalone prices for a set of media; flag whether any have a price."""
    total, priced = 0, False
    for u in uuids:
        p = _catalog_item(u).get("suggestedPriceCents", 0)
        if p > 0:
            priced = True
        total += p
    return total, priced


def bundle_price(bundle: dict, unowned: List[str]) -> int:
    """Price for the UNOWNED subset of a bundle. If the fan owns some members we
    charge a pro-rated share of the package price (by standalone value, or by
    count when members have no standalone price). Owning all members → 0."""
    items = bundle.get("items", [])
    full = bundle.get("bundlePriceCents", 0)
    if not unowned:
        return 0
    if set(unowned) == set(items):
        return full
    total, priced = _value_of(items)
    if priced and total > 0:
        sub, _ = _value_of(unowned)
        return round(full * sub / total)
    return round(full * len(unowned) / max(1, len(items)))


def best_bundle_offer(fan_id: str, mode: str, theme: str = "") -> Optional[dict]:
    """Pick the best themed bundle the fan can be offered right now, excluding
    members they already own. Returns {bundleId,title,items(unowned),price_cents} or None."""
    fan = get_fan(fan_id)
    level = MODE_LEVEL.get(mode, 1)
    bought = set(fan.get("purchasedUuids", []))
    offered = set(fan.get("recentlyOfferedUuids", []))
    q = (theme or "").lower().split()
    best, best_key = None, None
    for b in _backend().list(BUNDLES):
        if b.get("status") != "ready":
            continue
        if b.get("heat", 1) > level:
            continue
        unowned = [m for m in b.get("items", []) if m not in bought]
        if not unowned:
            continue  # fan already owns the whole set
        if all(m in offered for m in unowned):
            continue  # nothing new to show
        hay = (b.get("title", "") + " " + b.get("description", "") + " "
               + " ".join(b.get("tags", []))).lower()
        score = sum(1 for w in q if w in hay)
        key = (score, b.get("heat", 1))
        if best_key is None or key > best_key:
            best_key, best = key, (b, unowned)
    if not best:
        return None
    b, unowned = best
    return {"bundleId": b["bundleId"], "title": b.get("title", b["bundleId"]),
            "items": unowned, "price_cents": bundle_price(b, unowned)}


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


def record_offer(fan_id: str, media_uuids) -> None:
    """Record one or more media as offered (so they're not re-offered) and bump the
    per-session offer counter / PPV cooldown once.

    KNOWN LIMITATION (same-fan race): the check (cooldown_active in tools) and
    this write are a non-transactional read-modify-write on the fan doc. Two
    near-simultaneous messages from the same fan on different Cloud Run instances
    can both pass the gate before either records → one extra offer. Blast radius
    is tiny (MAX_OFFERS_PER_SESSION caps it) so we accept it for now; the correct
    fix is a Firestore transaction spanning cooldown-check + record_offer."""
    if isinstance(media_uuids, str):
        media_uuids = [media_uuids]
    fan = get_fan(fan_id)
    ro = fan.setdefault("recentlyOfferedUuids", [])
    for u in media_uuids:
        if u not in ro:
            ro.append(u)
    fan["lastPpvAt"] = _now().isoformat()
    fan["offersThisSession"] = fan.get("offersThisSession", 0) + 1
    save_fan(fan)


# ---------------- demo seed ----------------
def seed_demo_catalog() -> None:
    if _backend().list(CATALOG):
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
    add_catalog_item("demo-ppv-005", "playful cocktail-dress mirror set",
                     ["dress", "going-out", "flirty", "ppv"], heat=3,
                     suggested_price_cents=700)
    # A themed bundle: members also sell standalone; buying the set unlocks all.
    add_bundle("bundle:girls-night-out", "Girls Night Out",
               "a cheeky set from a wild night out with the girls — getting ready, drinks, dancing",
               ["girls", "night", "out", "party", "flirty", "ppv"], heat=3,
               items=["demo-ppv-004", "demo-ppv-005"], bundle_price_cents=1200)
