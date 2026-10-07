"""Event router — decides what each Fanvue webhook does.

Not every event runs the agent: a message does; a payment updates state (and can
trigger a proactive reaction); a new sub/follow just records the fan.
"""
from __future__ import annotations

from typing import Optional

import graph
import store

_processed: set[str] = set()  # idempotency (in-memory; Firestore in prod)


def already_processed(event_id: Optional[str]) -> bool:
    if not event_id:
        return False
    if event_id in _processed:
        return True
    _processed.add(event_id)
    return False


def handle_event(event: dict, *, dry_run: bool = False, proactive_on_purchase: bool = True) -> dict:
    etype = event.get("type", "")
    data = event.get("data", event)

    if already_processed(event.get("id") or event.get("eventId")):
        return {"status": "duplicate", "type": etype}

    if etype in ("creator.message.received", "message.received"):
        sender = data.get("sender", {})
        message = data.get("message", data)
        fan = sender.get("uuid") or data.get("senderUuid") or data.get("recipientUuid")
        text = message.get("text", "")
        if not fan or not text:
            return {"status": "ignored", "reason": "missing sender/text"}
        out = graph.handle_message(fan_id=fan, user_uuid=fan, text=text, dry_run=dry_run)
        return {"status": "replied", **out}

    if etype in ("creator.payment.succeeded", "payment.succeeded"):
        buyer = data.get("buyer", data.get("sender", {}))
        fan = buyer.get("uuid") or data.get("buyerUuid")
        invoice = data.get("invoiceId") or data.get("invoiceNumber") or event.get("id", "inv")
        gross = int(data.get("grossCents", data.get("amount", 0)))
        source = data.get("source", "unknown")
        media = data.get("mediaUuids", [])
        msg_uuid = data.get("messageUuid", "")
        if fan:
            store.add_purchase(fan, invoice, gross_cents=gross, source=source,
                               media_uuids=media, message_uuid=msg_uuid)
            if proactive_on_purchase:
                note = f"[fan just unlocked your paid content ({source}, ${gross/100:.2f}) — react warmly]"
                out = graph.handle_message(fan_id=fan, user_uuid=fan, text=note, dry_run=dry_run)
                return {"status": "purchase+reacted", "invoice": invoice, **out}
        return {"status": "purchase_recorded", "invoice": invoice}

    if etype in ("creator.subscription.activated", "subscription.activated"):
        sub = data.get("subscriber", data.get("sender", {}))
        fan = sub.get("uuid") or data.get("subscriberUuid")
        if fan:
            f = store.get_fan(fan)
            f["subscriptionStatus"] = "active"
            f["subscribedAt"] = store._now().isoformat()
            store.save_fan(f)
        return {"status": "subscriber_added"}

    if etype in ("creator.follow.created", "follow.created"):
        follower = data.get("follower", data.get("sender", {}))
        fan = follower.get("uuid")
        if fan:
            store.get_fan(fan)
        return {"status": "follower_added"}

    return {"status": "ignored", "type": etype}
