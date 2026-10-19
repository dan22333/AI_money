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

import contextlib
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


@contextlib.contextmanager
def namespace(ns: str):
    """Temporarily force the Firestore namespace, restoring it on exit. Used to
    pin cross-instance concerns (e.g. the Fanvue token-refresh lock) to the real
    DB regardless of which fan's request triggered them, so sim and real traffic
    contend on the SAME lock rather than two independent ones."""
    tok = _namespace.set(ns)
    try:
        yield
    finally:
        _namespace.reset(tok)

FANS = "fans"
CATALOG = "catalog"
BUNDLES = "bundles"
MESSAGES = "messages"
PURCHASES = "purchases"
PROCESSED = "processed_events"  # webhook idempotency ledger
SYNC_META = "sync_meta"         # per-source reconcile watermarks (delta sync)
LOCKS = "locks"                 # cross-instance named locks (e.g. token refresh)
FANVUE_TOKENS = "fanvue_tokens"  # one overwrite-in-place OAuth token doc per creator


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

    def update_atomic(self, col: str, doc_id: str, fn):
        """Read-modify-write a top-level doc. `fn(current|None) -> (new|None, ret)`:
        return a new dict to persist (None to leave untouched) and a value to
        return to the caller. Single-threaded in memory; a real transaction in
        Firestore (see that backend)."""
        cur = self.get(col, doc_id)
        new, ret = fn(cur)
        if new is not None:
            self.set(col, doc_id, new)
        return ret

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

    def sub_since(self, col, doc_id, sub, order_field, since, limit) -> List[dict]:
        vals = self._subs.get(self._skey(col, doc_id, sub), {}).values()
        rows = [copy.deepcopy(d) for d in vals
                if not since or str(d.get(order_field, "")) > since]
        rows.sort(key=lambda d: str(d.get(order_field, "")))
        return rows[:limit]

    def sub_count(self, col, doc_id, sub) -> int:
        return len(self._subs.get(self._skey(col, doc_id, sub), {}))

    def sub_items(self, col, doc_id, sub) -> List[tuple]:
        return [(k, copy.deepcopy(v))
                for k, v in self._subs.get(self._skey(col, doc_id, sub), {}).items()]

    def sub_delete(self, col, doc_id, sub, sub_id) -> None:
        self._subs.get(self._skey(col, doc_id, sub), {}).pop(_docid(sub_id), None)

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

    def update_atomic(self, col, doc_id, fn):
        """Transactional read-modify-write of one top-level doc. `fn` sees the
        current dict (or None) and returns (new_dict|None, ret). The read + write
        run inside one Firestore transaction, so concurrent Cloud Run instances
        can't both win (used for the per-fan processing lease)."""
        ref = self.db.collection(col).document(_docid(doc_id))
        fs = self._fs

        @fs.transactional
        def _txn(transaction):
            snap = ref.get(transaction=transaction)
            cur = snap.to_dict() if snap.exists else None
            new, ret = fn(cur)
            if new is not None:
                transaction.set(ref, new)
            return ret

        return _txn(self.db.transaction())

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

    def sub_since(self, col, doc_id, sub, order_field, since, limit):
        from google.cloud.firestore_v1.base_query import FieldFilter
        q = self._subref(col, doc_id, sub)
        if since:
            q = q.where(filter=FieldFilter(order_field, ">", since))
        q = q.order_by(order_field).limit(limit)  # range + order on same field → no composite index
        return [s.to_dict() for s in q.stream()]

    def sub_count(self, col, doc_id, sub):
        return sum(1 for _ in self._subref(col, doc_id, sub).stream())

    def sub_items(self, col, doc_id, sub):
        return [(s.id, s.to_dict()) for s in self._subref(col, doc_id, sub).stream()]

    def sub_delete(self, col, doc_id, sub, sub_id):
        self._subref(col, doc_id, sub).document(_docid(sub_id)).delete()

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
        "rollingSummary": "", "summarizedAtCount": 0,
        "purchasedUuids": [], "recentlyOfferedUuids": [],
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


def list_fans() -> List[dict]:
    """All fan docs in the CURRENT namespace. Used by reconcile to downgrade
    fans who unfollowed/unsubscribed (present in our store, absent from the live
    Fanvue list). Set the namespace before calling."""
    return _backend().list(FANS)


# ---------------- subscription / follow lifecycle ----------------
# We keep the CURRENT flag (subscriptionStatus / isFollower) denormalised on the
# fan for fast reads, AND an append-only `statusEvents` trace so a fan who goes
# active -> inactive -> active again is fully recoverable, not flattened to a
# single flag. Every transition is recorded exactly once (the mark_* helpers
# are no-ops when already in the target state), so webhooks (real-time) and the
# nightly reconcile diff can both call them without producing duplicates.
def append_status_event(fan: dict, event: str, *, at: str | None = None,
                        source: str = "reconcile") -> None:
    """Append one lifecycle event to the fan's trace. Mutates `fan`; caller saves."""
    fan.setdefault("statusEvents", []).append(
        {"event": event, "at": at or _now().isoformat(), "source": source})


def mark_subscribed(fan: dict, *, at: str | None = None, source: str = "reconcile") -> bool:
    """not-active -> active. Records a `subscribed` event and clears any stale
    unsubscribedAt. No-op (returns False) if already active."""
    if fan.get("subscriptionStatus") == "active":
        return False
    at = at or _now().isoformat()
    fan["subscriptionStatus"] = "active"
    fan["subscribedAt"] = at           # current subscription start (updates on re-sub)
    fan.pop("unsubscribedAt", None)
    append_status_event(fan, "subscribed", at=at, source=source)
    return True


def mark_unsubscribed(fan: dict, *, at: str | None = None, source: str = "reconcile") -> bool:
    """active -> expired. Records an `unsubscribed` event. No-op if not active."""
    if fan.get("subscriptionStatus") != "active":
        return False
    at = at or _now().isoformat()
    fan["subscriptionStatus"] = "expired"
    fan["unsubscribedAt"] = at
    append_status_event(fan, "unsubscribed", at=at, source=source)
    return True


def mark_followed(fan: dict, *, at: str | None = None, source: str = "reconcile") -> bool:
    """not-follower -> follower. Records a `followed` event. No-op if already following."""
    if fan.get("isFollower"):
        return False
    at = at or _now().isoformat()
    fan["isFollower"] = True
    fan["followedAt"] = at
    fan.pop("unfollowedAt", None)
    append_status_event(fan, "followed", at=at, source=source)
    return True


def mark_unfollowed(fan: dict, *, at: str | None = None, source: str = "reconcile") -> bool:
    """follower -> not-follower. Records an `unfollowed` event. No-op if not following."""
    if not fan.get("isFollower"):
        return False
    at = at or _now().isoformat()
    fan["isFollower"] = False
    fan["unfollowedAt"] = at
    append_status_event(fan, "unfollowed", at=at, source=source)
    return True


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
                 price_cents: int = 0, mode: str = "", session_id: str = "",
                 via: str = "webhook", kind: str = "") -> None:
    doc = {"fan_id": fan_id, "role": role, "text": text, "kind": kind,
           "mediaUuids": media_uuids or [], "priceCents": price_cents,
           "mode": mode, "sessionId": session_id, "ts": _now().isoformat(),
           "via": via,  # how this message entered our store: "webhook" | "reconcile"
           "source": "sim" if fan_id.startswith("sim:") else "fanvue"}
    _backend().sub_add(FANS, fan_id, MESSAGES, doc)


def upsert_message(fan_id: str, msg_uuid: str, role: str, text: str, *,
                   sent_at: str = "", media_uuids=None, price_cents: int = 0,
                   mode: str = "", session_id: str = "", via: str = "webhook",
                   kind: str = "") -> bool:
    """Idempotent message store keyed by Fanvue messageUuid. Used by BOTH the
    nightly backfill AND the live webhook path (fan inbound keyed by the webhook's
    messageUuid, Jenny's reply keyed by the uuid the send API returns) so the two
    paths write the SAME doc id — re-running reconcile over already-live messages
    collides and no-ops instead of creating a second, random-id copy. Returns True
    if newly written. `ts` is the real sent time so it interleaves correctly with
    other messages in recent_messages()."""
    if not msg_uuid:
        return False
    if _backend().sub_get(FANS, fan_id, MESSAGES, msg_uuid) is not None:
        return False
    _backend().sub_set(FANS, fan_id, MESSAGES, msg_uuid,
                       {"fan_id": fan_id, "role": role, "text": text, "messageUuid": msg_uuid,
                        "kind": kind,
                        "mediaUuids": media_uuids or [], "priceCents": price_cents,
                        "mode": mode, "sessionId": session_id,
                        "ts": sent_at or _now().isoformat(),
                        "via": via,  # "webhook" (live) | "reconcile" (backfill)
                        "source": "sim" if fan_id.startswith("sim:") else "fanvue"})
    return True


def recent_messages(fan_id: str, limit: int = 15) -> List[dict]:
    return _backend().sub_recent(FANS, fan_id, MESSAGES, "ts", limit)


def dedup_live_messages(fan_id: str, *, apply: bool = False) -> dict:
    """One-time cleanup of the pre-fix double-write. Before messageUuid was
    threaded through the live path, every live webhook message was stored under a
    RANDOM doc id with no messageUuid, and the nightly reconcile then re-pulled the
    same message and stored a SECOND copy keyed by messageUuid. This removes the
    random-id copies that are now shadowed by a uuid-keyed twin (same role + text),
    keeping the canonical uuid-keyed doc. Random-id docs with NO uuid-keyed twin
    (e.g. a reply that never came back from a reconcile pull) are left untouched.

    Dry-run by default: returns {examined, duplicates, deleted}. Pass apply=True to
    actually delete. Set the namespace (use_namespace_for) before calling."""
    items = _backend().sub_items(FANS, fan_id, MESSAGES)
    # canonical keys that exist as a uuid-keyed doc
    keyed = {(d.get("role"), (d.get("text") or "").strip())
             for _id, d in items if d.get("messageUuid")}
    deleted = 0
    duplicates = 0
    for sub_id, d in items:
        if d.get("messageUuid"):
            continue  # this IS the canonical copy
        if (d.get("role"), (d.get("text") or "").strip()) in keyed:
            duplicates += 1
            if apply:
                _backend().sub_delete(FANS, fan_id, MESSAGES, sub_id)
                deleted += 1
    return {"fan": fan_id, "examined": len(items), "duplicates": duplicates, "deleted": deleted}


def messages_since(fan_id: str, since: str = "", limit: int = 1000) -> List[dict]:
    """Stored messages with ts strictly after `since` (ISO string), oldest first,
    capped at `limit`. `since` empty → from the beginning. Used by the mem0
    backfill to feed only not-yet-extracted history (watermarked); resumable —
    a run that hits the cap advances the watermark and the next run continues."""
    return _backend().sub_since(FANS, fan_id, MESSAGES, "ts", since, limit)


def message_count(fan_id: str) -> int:
    return _backend().sub_count(FANS, fan_id, MESSAGES)


# ---------------- per-fan debounce (batch + lock + watermark) ----------------
# Two fields on the fan doc drive the whole mechanism (see docs/debounce):
#   processingLease = None | {"owner", "expiresAt"}  — the lock (one worker/fan)
#   lastProcessedAt = ISO ts                          — watermark: newest fan
#                     message we've already replied to; anything newer is pending.
def record_inbound(fan_id: str, msg_uuid: str, text: str) -> None:
    """Debounce inbound: store the fan's message (idempotent by messageUuid)
    WITHOUT running the agent — the worker replies later. We deliberately do NOT
    touch lastSeenAt here: the worker's compute_session needs the pre-burst value
    to detect a new session, and bumping it on every inbound would erase that gap.

    First-engagement baseline: if this fan has no watermark yet (debounce just
    turned on, or a fan who only ever ran the synchronous path), seed it to our
    LAST REPLY's timestamp so pre-existing, already-handled history isn't treated
    as one giant unanswered batch. Trailing fan messages newer than our last reply
    stay pending — same rule reconcile uses. Only runs while the watermark is unset."""
    if not (get_fan(fan_id).get("lastProcessedAt")):
        hist = recent_messages(fan_id, limit=50)
        last_jenny = max((m.get("ts") or "" for m in hist if m.get("role") == "jenny"),
                         default="")
        if last_jenny:
            init_watermark_if_unset(fan_id, last_jenny)
    if msg_uuid:
        upsert_message(fan_id, msg_uuid, "fan", text, via="webhook")
    else:
        save_message(fan_id, "fan", text, via="webhook")


def init_watermark_if_unset(fan_id: str, ts: str) -> None:
    """Set lastProcessedAt to `ts` only if it's currently unset (atomic check-and-
    set). Used to baseline a fan the first time debounce engages; never overwrites
    a watermark the live worker is already managing."""
    if not ts:
        return

    def fn(fan):
        if fan is None:
            fan = _default_fan(fan_id)
        if fan.get("lastProcessedAt"):
            return None, None  # already managed — leave it
        fan["lastProcessedAt"] = ts
        return fan, None

    _backend().update_atomic(FANS, fan_id, fn)


def unanswered_fan_messages(fan_id: str, limit: int = 1000) -> List[dict]:
    """Fan messages newer than the watermark (role == 'fan', ts > lastProcessedAt),
    oldest first — the batch a worker must answer. Jenny's own replies never count,
    and neither do synthetic internal prompts (kind == 'system', e.g. the purchase
    reaction note) — those aren't real fan utterances to respond to."""
    wm = (get_fan(fan_id).get("lastProcessedAt")) or ""
    return [m for m in messages_since(fan_id, wm, limit=limit)
            if m.get("role") == "fan" and m.get("kind") != "system"]


def acquire_lease(fan_id: str, owner: str, ttl_seconds: int) -> bool:
    """Atomically grab the per-fan processing lease. True if acquired (nobody held
    it, OR the previous holder's lease expired — a dead worker self-heals), False
    if a live worker currently holds it."""
    now = _now()
    expires = (now + timedelta(seconds=ttl_seconds)).isoformat()
    now_iso = now.isoformat()

    def fn(fan):
        if fan is None:
            fan = _default_fan(fan_id)
        lease = fan.get("processingLease")
        if lease and lease.get("owner") != owner and (lease.get("expiresAt") or "") > now_iso:
            return None, False  # held by a live worker
        fan["processingLease"] = {"owner": owner, "expiresAt": expires}
        return fan, True

    return _backend().update_atomic(FANS, fan_id, fn)


def finish_processing(fan_id: str, owner: str, through_ts: str) -> None:
    """Advance the watermark to `through_ts` (monotonic) and release the lease, in
    one transaction — but only clear the lease if we still own it (a lease we lost
    to expiry is someone else's now)."""
    def fn(fan):
        if fan is None:
            return None, None
        cur = fan.get("lastProcessedAt") or ""
        if through_ts and through_ts > cur:
            fan["lastProcessedAt"] = through_ts
        lease = fan.get("processingLease")
        if lease and lease.get("owner") == owner:
            fan["processingLease"] = None
        return fan, None

    _backend().update_atomic(FANS, fan_id, fn)


def release_lease(fan_id: str, owner: str) -> None:
    """Drop the lease without moving the watermark (used when a worker bails out
    before replying — e.g. nothing to answer, or a generation error). Only clears
    a lease we still own."""
    def fn(fan):
        if fan is None:
            return None, None
        lease = fan.get("processingLease")
        if lease and lease.get("owner") == owner:
            fan["processingLease"] = None
            return fan, None
        return None, None

    _backend().update_atomic(FANS, fan_id, fn)


def acquire_lock(name: str, owner: str, ttl_seconds: int) -> bool:
    """Atomically grab a named cross-instance lock. True if acquired (free, or the
    previous holder's lease expired → a dead holder self-heals), False if a live
    owner still holds it. Generic sibling of acquire_lease, keyed by an arbitrary
    name instead of a fan id — used to serialize Fanvue OAuth token refresh across
    Cloud Run instances so the single-use refresh token isn't rotated twice at once."""
    now = _now()
    expires = (now + timedelta(seconds=ttl_seconds)).isoformat()
    now_iso = now.isoformat()

    def fn(cur):
        if cur and cur.get("owner") != owner and (cur.get("expiresAt") or "") > now_iso:
            return None, False  # held by a live owner
        return {"owner": owner, "expiresAt": expires}, True

    return _backend().update_atomic(LOCKS, name, fn)


def release_lock(name: str, owner: str) -> None:
    """Release a named lock, but only if we still own it (a lease we lost to expiry
    now belongs to whoever took it over)."""
    def fn(cur):
        if cur and cur.get("owner") == owner:
            return {"owner": None, "expiresAt": None}, None
        return None, None

    _backend().update_atomic(LOCKS, name, fn)


def get_fanvue_token(creator_id: str) -> Optional[dict]:
    """Read the stored OAuth token doc for one creator (None if absent)."""
    return _backend().get(FANVUE_TOKENS, creator_id)


def set_fanvue_token(creator_id: str, data: dict) -> None:
    """Overwrite the OAuth token doc for one creator in place (no versioning)."""
    _backend().set(FANVUE_TOKENS, creator_id, data)


def list_fanvue_token_creator_ids() -> List[str]:
    """Every creator id that has a stored token doc (for the proactive refresher
    to iterate). Relies on each doc carrying its own creatorId field."""
    return [d.get("creatorId") for d in _backend().list(FANVUE_TOKENS) if d.get("creatorId")]


def merge_fan_fields(fan_id: str, fields: dict) -> None:
    """Atomically set specific fields on the fan doc WITHOUT overwriting the rest.
    Needed for writes that happen after the lease is released (e.g. the rolling
    summary): a plain save_fan would overwrite the whole doc and could clobber a
    processingLease / lastProcessedAt that a concurrent worker just set."""
    if not fields:
        return

    def fn(fan):
        if fan is None:
            fan = _default_fan(fan_id)
        fan.update(fields)
        return fan, None

    _backend().update_atomic(FANS, fan_id, fn)


def advance_watermark_to(fan_id: str, ts: str) -> None:
    """Move the watermark forward to `ts` if it's newer (monotonic; never backward).
    Used by reconcile to mark backfilled history up to our last reply as answered,
    so a webhook-triggered worker doesn't try to re-answer the whole imported chat."""
    if not ts:
        return

    def fn(fan):
        if fan is None:
            fan = _default_fan(fan_id)
        cur = fan.get("lastProcessedAt") or ""
        if ts > cur:
            fan["lastProcessedAt"] = ts
            return fan, None
        return None, None

    _backend().update_atomic(FANS, fan_id, fn)


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


# ---------------- reconcile watermarks (delta sync) ----------------
def get_sync_meta(key: str) -> dict:
    """Read a per-source sync watermark (e.g. last earnings date, per-chat last
    message time). Namespaced like everything else, so sim/real stay separate."""
    return _backend().get(SYNC_META, key) or {}


def set_sync_meta(key: str, value: dict) -> None:
    _backend().set(SYNC_META, key, value)


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
