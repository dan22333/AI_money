"""Event router — decides what each Fanvue webhook does.

Not every event runs the agent: a message does; a payment updates state (and can
trigger a proactive reaction); a new sub/follow just records the fan.
"""
from __future__ import annotations

from typing import Optional

import graph
import store


def already_processed(event_id: Optional[str]) -> bool:
    """Durable, cross-instance dedup via store.claim_event (Firestore atomic
    create in prod). Returns True if this event was already handled.

    Note: events without an id can't be deduped — Fanvue always sends one."""
    if not event_id:
        return False
    return not store.claim_event(event_id)


def handle_event(event: dict, *, dry_run: bool = False, proactive_on_purchase: bool = True) -> dict:
    etype = event.get("type", "")
    data = event.get("data", event)
    # Activation observability: real Fanvue payload shapes were never verified
    # against the live API, so log the envelope + (for messages) the parsed
    # fields so a silent no-reply is instantly diagnosable. Trim later.
    print(f"[webhook] type={etype!r} top_keys={list(event.keys())} "
          f"data_keys={list(data.keys()) if isinstance(data, dict) else type(data).__name__}")

    if already_processed(event.get("id") or event.get("eventId")):
        return {"status": "duplicate", "type": etype}

    # Legacy Fanvue message webhooks are FLAT and carry NO `type` field — the
    # kind is implied by the payload shape (has `message` + `sender`/`messageUuid`).
    # Modern creator.* events use a type + data envelope. Detect either way.
    is_message = etype in ("creator.message.received", "message.received") or (
        isinstance(data, dict) and "message" in data
        and ("sender" in data or "messageUuid" in data)
    )
    if is_message:
        sender = data.get("sender") or {}
        if not isinstance(sender, dict):
            sender = {}
        message = data.get("message") or {}
        if not isinstance(message, dict):
            message = {"text": message}
        # the fan is the SENDER (never recipientUuid — that's Jenny herself)
        fan = (sender.get("uuid") or sender.get("userUuid") or sender.get("id")
               or data.get("senderUuid"))
        text = message.get("text") or message.get("content") or message.get("body") or ""
        # messageUuid identifies THIS inbound message — thread it through so the
        # live write is keyed by it and the nightly reconcile re-pull no-ops
        # instead of storing a duplicate (#3). Enveloped shape puts it on `message`,
        # legacy flat shape puts it at the top level.
        fan_msg_uuid = (message.get("uuid") or message.get("messageUuid")
                        or data.get("messageUuid") or "")
        print(f"[webhook] message parsed fan={fan!r} text_len={len(text) if text else 0} "
              f"uuid={fan_msg_uuid!r} sender_keys={list(sender.keys())} "
              f"message_keys={list(message.keys())}")
        if not fan or not text:
            print("[webhook] IGNORED message: could not find sender uuid and/or text "
                  "— real payload shape differs from the parse above")
            return {"status": "ignored", "reason": "missing sender/text"}
        out = graph.handle_message(fan_id=fan, user_uuid=fan, text=text,
                                   fan_msg_uuid=fan_msg_uuid, dry_run=dry_run)
        print(f"[webhook] replied mode={out.get('mode')} reply_len={len(out.get('reply') or '')} "
              f"actions={len(out.get('actions') or [])}")
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
            store.use_namespace_for(fan)
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
            store.use_namespace_for(fan)
            f = store.get_fan(fan)
            store.mark_subscribed(f, source="webhook")  # records a lifecycle event
            store.save_fan(f)
        return {"status": "subscriber_added"}

    if etype in ("creator.follow.created", "follow.created"):
        follower = data.get("follower", data.get("sender", {}))
        fan = follower.get("uuid")
        if fan:
            store.use_namespace_for(fan)
            f = store.get_fan(fan)
            store.mark_followed(f, source="webhook")  # also sets isFollower (was missing)
            store.save_fan(f)
        return {"status": "follower_added"}

    return {"status": "ignored", "type": etype}
