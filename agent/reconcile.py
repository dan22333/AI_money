"""Nightly reconcile — repair state from the Fanvue API.

Webhooks are the primary path, but they can be missed (downtime, delivery
failures, races). Once a night Cloud Scheduler POSTs /admin/reconcile, which
runs this: pull the source-of-truth from Fanvue and upsert it idempotently.

  - subscribers → ensure a fan row exists + subscriptionStatus=active
  - earnings    → store.add_purchase (idempotent by invoice), so spend/tier/
                  owned-media stay correct even if a payment webhook was lost

Everything here targets the PRODUCTION namespace (real fans only). It's pull +
idempotent-upsert, so running it twice is harmless. If Fanvue isn't connected
yet (no tokens), it no-ops rather than erroring.
"""
from __future__ import annotations

import store
from fanvue_client import fanvue


def _sync_subscribers() -> int:
    n = 0
    for sub in fanvue.list_subscribers():
        fan_id = sub.get("uuid") or sub.get("fanUuid")
        if not fan_id:
            continue
        store.use_namespace_for(fan_id)  # real fans → prod DB
        fan = store.get_fan(fan_id)
        fan["subscriptionStatus"] = "active"
        if sub.get("subscribedAt"):
            fan.setdefault("subscribedAt", sub["subscribedAt"])
        store.save_fan(fan)
        n += 1
    return n


def _sync_earnings(since: str | None) -> int:
    n = 0
    for e in fanvue.list_earnings(since=since):
        fan_id = e.get("fanUuid") or e.get("buyerUuid") or (e.get("buyer") or {}).get("uuid")
        invoice = e.get("invoiceId") or e.get("invoiceNumber")
        if not fan_id or not invoice:
            continue
        store.use_namespace_for(fan_id)
        store.add_purchase(
            fan_id, invoice,
            gross_cents=int(e.get("grossCents", e.get("amount", 0))),
            source=e.get("source", "reconcile"),
            media_uuids=e.get("mediaUuids", []),
            message_uuid=e.get("messageUuid", ""),
        )
        n += 1
    return n


def run(since: str | None = None) -> dict:
    """Run the reconcile. Returns a summary dict (also the HTTP response body)."""
    if not fanvue.ready:
        return {"status": "skipped", "reason": "fanvue not connected"}
    subs = _sync_subscribers()
    earns = _sync_earnings(since)
    return {"status": "ok", "subscribersSynced": subs, "purchasesSynced": earns}
