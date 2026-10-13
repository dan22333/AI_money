"""Reconcile / backfill state from the Fanvue API.

Webhooks are the primary, real-time path, but they can be missed (downtime,
delivery failures, races) and they don't cover history. This pulls the
source-of-truth from Fanvue and upserts it idempotently into our store:

  - fans      ← /v1/followers + /v1/subscribers (merged by uuid; follower/sub
                flags live on the ONE fan row, so no double-counting)
  - purchases ← /v1/insights/earnings (idempotent by transactionOrderId; $0
                rows like free follows/subs are not money, so skipped)
  - messages  ← per-chat history BOTH directions (/v1/chats → /v1/chats/{uuid}/
                messages), idempotent by Fanvue messageUuid
  - catalog   ← the creator's own media library (/v1/media) so Jenny has
                inventory to tease/sell

Delta sync: watermarks in store.sync_meta let re-runs pull only NEW data
(earnings after the last date; per-chat messages after the last sentAt).
Re-running is always safe — every write is keyed (uuid / transactionOrderId /
messageUuid). Endpoints/shapes verified live via /admin/fanvue_probe.

Targets the PRODUCTION namespace (real fans). No-ops if Fanvue isn't connected.
"""
from __future__ import annotations

import store
from fanvue_client import fanvue


def _creator_uuid() -> str:
    """The creator's own uuid — used to label message direction (jenny vs fan)."""
    try:
        return fanvue.get_me().get("uuid", "")
    except Exception:
        return ""


def _upsert_fan(uuid: str, *, is_follower: bool | None = None, is_subscriber: bool = False,
                handle: str | None = None, display_name: str | None = None,
                registered_at: str | None = None) -> None:
    """Merge a fan by uuid — follower and subscriber are independent flags on the
    SAME row, so someone who is both is one record with both set."""
    store.use_namespace_for(uuid)  # real fans → prod DB
    fan = store.get_fan(uuid)
    if handle:
        fan["handle"] = handle
    if display_name:
        fan["displayName"] = display_name
    if registered_at:
        fan.setdefault("registeredAt", registered_at)
    if is_follower:
        fan["isFollower"] = True
        fan.setdefault("followedAt", registered_at or store._now().isoformat())
    if is_subscriber:
        fan["subscriptionStatus"] = "active"
        fan.setdefault("subscribedAt", registered_at or store._now().isoformat())
    store.save_fan(fan)


def _downgrade_dropped_fans(follower_ids: set, subscriber_ids: set) -> int:
    """Clear flags for fans who LEFT — present in our store but absent from the
    live Fanvue list means they unfollowed / unsubscribed. Fanvue's list IS the
    source of truth (someone who cancels just drops off `/v1/subscribers`), so a
    full re-pull + diff is how we learn it; there's no reliable cancel webhook.

    SAFETY: only correct because list_followers/list_subscribers are now fully
    paginated (complete). A truncated list would wrongly downgrade everyone past
    page 1 — so this must never run on a partial fetch."""
    store.use_namespace_for("__reconcile__")  # select the prod namespace (non-sim)
    downgraded = 0
    for fan in store.list_fans():
        uuid = fan.get("fanUuid")
        if not uuid or str(uuid).startswith("sim:"):
            continue
        changed = False
        if fan.get("isFollower") and uuid not in follower_ids:
            fan["isFollower"] = False
            fan["unfollowedAt"] = store._now().isoformat()
            changed = True
        if fan.get("subscriptionStatus") == "active" and uuid not in subscriber_ids:
            fan["subscriptionStatus"] = "expired"
            fan["unsubscribedAt"] = store._now().isoformat()
            changed = True
        if changed:
            store.save_fan(fan)
            downgraded += 1
    return downgraded


def _sync_fans() -> dict:
    """Upsert followers + subscribers, then downgrade anyone who dropped off the
    live lists (unfollow/unsubscribe). Returns processed + downgraded counts."""
    follower_ids: set = set()
    subscriber_ids: set = set()
    n = 0
    for f in fanvue.list_followers():
        uuid = f.get("uuid")
        if not uuid:
            continue
        follower_ids.add(uuid)
        _upsert_fan(uuid, is_follower=True, handle=f.get("handle"),
                    display_name=f.get("displayName"), registered_at=f.get("registeredAt"))
        n += 1
    for s in fanvue.list_subscribers():
        uuid = s.get("uuid") or s.get("fanUuid")
        if not uuid:
            continue
        subscriber_ids.add(uuid)
        _upsert_fan(uuid, is_subscriber=True, handle=s.get("handle"),
                    display_name=s.get("displayName"), registered_at=s.get("subscribedAt"))
        n += 1
    downgraded = _downgrade_dropped_fans(follower_ids, subscriber_ids)
    return {"fansSynced": n, "fansDowngraded": downgraded}


def _sync_earnings() -> int:
    """Upsert real purchases (gross > 0). $0 rows (free follows/subs) are not
    money and would mis-tier a fan, so they're skipped. Delta by last date."""
    wm = store.get_sync_meta("earnings")
    since = wm.get("lastDate")
    latest = since
    n = 0
    for e in fanvue.list_earnings(since=since):
        user = e.get("user") or {}
        uuid = user.get("uuid") or e.get("fanUuid")
        invoice = e.get("transactionOrderId") or e.get("invoiceNumber") or e.get("invoiceId")
        date = e.get("date")
        if date and (not latest or date > latest):
            latest = date
        gross = int(e.get("gross") or 0)
        if not uuid or not invoice or gross <= 0:
            continue  # skip incomplete rows and $0 (follows/free subs aren't spend)
        store.use_namespace_for(uuid)
        store.add_purchase(uuid, invoice, gross_cents=gross, source=e.get("source", "reconcile"))
        n += 1
    if latest and latest != since:
        store.set_sync_meta("earnings", {"lastDate": latest})
    return {"purchasesSynced": n}


def _sync_messages(creator_uuid: str) -> dict:
    """Backfill full chat history (both directions), idempotent by messageUuid.
    Per-chat delta via the last sentAt watermark."""
    n = 0
    for chat in fanvue.list_chats(unread_only=False):
        user = chat.get("user") or {}
        uuid = user.get("uuid")
        if not uuid:
            continue
        store.use_namespace_for(uuid)
        wm = store.get_sync_meta(f"chat:{uuid}")
        since = wm.get("lastSentAt")
        latest = since
        for m in fanvue.list_chat_messages(uuid, since=since):
            mid = m.get("uuid")
            if not mid:
                continue
            sender = (m.get("sender") or {}).get("uuid")
            role = "jenny" if (sender and sender == creator_uuid) else "fan"
            sent = m.get("sentAt", "")
            if store.upsert_message(uuid, mid, role, m.get("text") or "", sent_at=sent):
                n += 1
            if sent and (not latest or sent > latest):
                latest = sent
        if latest and latest != since:
            store.set_sync_meta(f"chat:{uuid}", {"lastSentAt": latest})
    return {"messagesSynced": n}


def _sync_media() -> dict:
    """Backfill the creator's media library into the catalog (teasers/selling).
    NOTE: recommendedPrice unit is stored as-is pending confirmation."""
    store.use_namespace_for("catalog")  # catalog is global (real namespace)
    n = 0
    for m in fanvue.list_media():
        uuid = m.get("uuid")
        if not uuid:
            continue
        desc = m.get("description") or m.get("caption") or m.get("name") or ""
        store.add_catalog_item(uuid, desc, tags=m.get("tags") or [], heat=2,
                               suggested_price_cents=int(m.get("recommendedPrice") or 0),
                               meta_source="fanvue",
                               status="ready" if m.get("status") in (None, "ready", "processed", "completed")
                               else str(m.get("status")))
        n += 1
    return {"mediaSynced": n}


def run(since: str | None = None) -> dict:
    """Run every sync, isolated so one failing source can't discard the others.
    Returns a summary (also the HTTP response body)."""
    if not fanvue.ready:
        return {"status": "skipped", "reason": "fanvue not connected"}
    creator = _creator_uuid()
    result: dict = {"status": "ok"}
    errors: dict = {}
    for name, fn in (("fans", _sync_fans),
                     ("purchases", _sync_earnings),
                     ("messages", lambda: _sync_messages(creator)),
                     ("media", _sync_media)):
        try:
            result.update(fn())  # each sync returns its own {xSynced: n, ...} counts
        except Exception as e:
            errors[name] = f"{type(e).__name__}: {e}"
    if errors:
        result["status"] = "partial"
        result["errors"] = errors
    return result
