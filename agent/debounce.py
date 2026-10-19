"""The per-fan debounce worker — the body of POST /admin/process_fan.

Cloud Tasks fires this ~SETTLE seconds after a fan message. It answers the fan's
whole unanswered batch in ONE reply, under a per-fan lock, exactly once. The flow:

  1. read the unanswered batch (fan messages past the watermark); nothing → done
  2. quiet check: if a newer message arrived < SETTLE ago, bail — that message's
     own task will do the work (only the LAST message's task finds the fan quiet)
  3. grab the per-fan lease (lock); held by a live worker → re-enqueue + bail
  4. re-read under the lock, fix the batch boundary (through_ts = newest fan ts)
  5. retry-only read-back: if a crashed earlier attempt already sent a reply for
     this batch (confirmed against Fanvue), skip the send and just advance
  6. generate → send → record (send-first ordering in graph._deliver_and_record)
  7. commit: advance the watermark to through_ts + release the lease (atomic)
  8. tail check: messages that landed during generation → enqueue a follow-up

Idempotency/consistency reasoning lives in docs/debounce; the short version: a
crash can only ever cause a rare DUPLICATE reply (never a lost one), and the
retry read-back removes even that except for Fanvue's own read-after-write lag.
"""
from __future__ import annotations

import os
import socket
import uuid
from datetime import datetime, timezone

import graph
import store
import tasks
from config import settings


def _owner_id() -> str:
    """A unique id for THIS worker attempt, so finish/release only ever clears a
    lease we still hold (not one a later worker took over after we were presumed
    dead)."""
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def _seconds_since(ts_iso: str) -> float:
    """Seconds between an ISO timestamp and now (UTC). Large if unparseable so we
    err toward 'quiet' (answer) rather than stalling forever."""
    try:
        ts = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        return 1e9


def _jenny_replied_since(user_uuid: str, through_ts: str) -> bool:
    """Ask Fanvue (the source of truth) whether a Jenny reply already landed after
    `through_ts` — i.e. a previous crashed attempt for THIS batch did send. A fan's
    own newer message is excluded (its sender is the fan, not Jenny). This is what
    makes a retry safe: we trust Fanvue's state, not a local marker that could
    itself have failed to persist."""
    try:
        from fanvue_client import fanvue
        if not fanvue.ready:
            return False
        for m in fanvue.list_chat_messages(user_uuid, since=through_ts):
            sender = m.get("sender")
            suid = sender.get("uuid") if isinstance(sender, dict) else sender
            sent = m.get("sentAt") or ""
            if sent > through_ts and suid and suid != user_uuid:
                return True
    except Exception as e:  # read-back failure → don't skip (worst case: a resend)
        print(f"[debounce] read-back check failed: {type(e).__name__}: {e}")
    return False


def process_fan(fan_id: str, user_uuid: str, *, is_retry: bool = False) -> dict:
    store.use_namespace_for(fan_id)
    settle = settings.DEBOUNCE_SETTLE_SECONDS

    # 1 + 2: cheap pre-lock read + quiet check
    batch = store.unanswered_fan_messages(fan_id)
    if not batch:
        return {"status": "nothing", "fan": fan_id}
    newest_ts = max((m.get("ts") or "") for m in batch)
    quiet_for = _seconds_since(newest_ts)
    if quiet_for < settle:
        # Not settled yet. Cloud Tasks dispatch has jitter and can fire a task
        # slightly EARLY (observed ~2.85s for a 3s settle), so even the LAST
        # message's own task can land just under `settle`. If we only bailed, that
        # last message would never be answered — no later task exists to pick it up
        # (the "answer gap" bug). So RE-ENQUEUE a short follow-up: a batch is always
        # eventually processed regardless of jitter. Idempotent — once a reply lands
        # the watermark advances and any redundant task finds "nothing". +1s buffer
        # so the retry lands comfortably past the settle window, not early again.
        tasks.enqueue_process_fan(fan_id, user_uuid, max(settle - quiet_for, 0.0) + 1.0)
        return {"status": "not_quiet", "fan": fan_id, "quietFor": round(quiet_for, 2)}

    # 3: acquire the per-fan lock
    owner = _owner_id()
    if not store.acquire_lease(fan_id, owner, settings.PROCESS_LEASE_TTL_SECONDS):
        tasks.enqueue_process_fan(fan_id, user_uuid, settings.PROCESS_BUSY_RETRY_SECONDS)
        return {"status": "busy", "fan": fan_id}

    try:
        # 4: re-read under the lock — the watermark/batch may have changed between
        # the pre-lock read and acquiring the lease.
        batch = store.unanswered_fan_messages(fan_id)
        if not batch:
            store.release_lease(fan_id, owner)
            return {"status": "nothing_after_lock", "fan": fan_id}
        through_ts = max((m.get("ts") or "") for m in batch)

        # 5: retry-only read-back — did an earlier crashed attempt already send?
        if is_retry and _jenny_replied_since(user_uuid, through_ts):
            store.finish_processing(fan_id, owner, through_ts)
            return {"status": "already_sent", "fan": fan_id, "through": through_ts}

        # 6: generate + send + store the reply (one reply for the whole batch). The
        # heavy mem0 extraction + summary are DEFERRED (not run inside handle_batch).
        batch_text = "\n".join((m.get("text") or "") for m in batch)
        out = graph.handle_batch(fan_id, user_uuid, through_ts, batch_text)

        # 7: commit — advance watermark + release lease atomically
        store.finish_processing(fan_id, owner, through_ts)
    except Exception:
        # leave the watermark untouched and drop the lease so a retry regenerates;
        # re-raise so the handler returns non-2xx and Cloud Tasks redelivers.
        store.release_lease(fan_id, owner)
        raise

    # 8: tail check — messages that arrived mid-generation get their own follow-up
    tail = store.unanswered_fan_messages(fan_id)
    if tail:
        tasks.enqueue_process_fan(fan_id, user_uuid, settle)

    # 9: post-reply bookkeeping OUTSIDE the lease — the fan already has the reply and
    # the lock is released, so mem0 fact-extraction (~1-3s) + summary don't block the
    # next batch for this fan. maybe_learn enqueues it (fan-out) or runs it inline.
    # Best-effort: a failure here never loses the reply or the watermark (reconcile
    # re-extracts facts from stored history).
    try:
        graph.maybe_learn(fan_id, batch_text, out.get("reply") or "")
    except Exception as e:
        print(f"[debounce] post-reply learn failed (non-fatal): {type(e).__name__}: {e}")

    return {"status": "replied", "fan": fan_id, "through": through_ts,
            "batchSize": len(batch), "reply_len": len(out.get("reply") or "")}
