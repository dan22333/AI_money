"""Cloud Tasks enqueue for the per-fan debounce worker.

The webhook records a fan message then calls enqueue_process_fan(), which schedules
a delayed POST to /admin/process_fan. Cloud Tasks is Google's "call this URL later,
reliably" service: it holds the job until its schedule time, fires the POST, and
retries (with backoff, up to the queue's limits) until the handler returns 2xx.

Enqueue is a no-op (returns False) unless debounce is on AND a queue + service URL
are configured — the caller then falls back to the synchronous path, so nothing
breaks in local dev / tests where Cloud Tasks isn't wired up.
"""
from __future__ import annotations

import json

from config import settings


def _can_enqueue(queue: str) -> bool:
    """We can enqueue only if a queue name + the service's own URL are configured
    (and we're on real infra). Otherwise the caller falls back to inline work."""
    return bool(queue and settings.SERVICE_BASE_URL and settings.USE_FIRESTORE)


def enabled() -> bool:
    """True only if debounce is on and we can actually enqueue (queue + URL set)."""
    return bool(settings.DEBOUNCE_ENABLED and _can_enqueue(settings.TASKS_QUEUE))


def fanout_enabled() -> bool:
    """True only if extraction fan-out is on and the extract queue is wired up."""
    return bool(settings.MEMORY_FANOUT_ENABLED and _can_enqueue(settings.EXTRACT_TASKS_QUEUE))


def _enqueue(queue: str, path: str, body: dict, delay_seconds: float = 0.0,
             name: str | None = None) -> bool:
    """Create one Cloud Tasks HTTP job: POST `path` on our own service with `body`,
    ~delay_seconds from now. Returns True if enqueued, False on any failure (caller
    falls back to inline). Never raises.

    `name` is an optional dedup id: Cloud Tasks rejects a second task with the same
    name for ~1h after the first completes, so a redelivered webhook can't schedule
    the job twice. AlreadyExists is therefore a SUCCESS (the job is already queued),
    not a failure."""
    try:
        from google.cloud import tasks_v2
        from google.protobuf import timestamp_pb2

        client = tasks_v2.CloudTasksClient()
        parent = client.queue_path(settings.GCP_PROJECT, settings.TASKS_LOCATION, queue)
        headers = {"Content-Type": "application/json"}
        if settings.SIM_SECRET:
            # the worker endpoints share the same sim-secret as the other /admin
            # routes; attach it so the task is authorized when it fires.
            headers["x-sim-secret"] = settings.SIM_SECRET
        http_request: dict = {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": settings.SERVICE_BASE_URL.rstrip("/") + path,
            "headers": headers,
            "body": json.dumps(body).encode(),
        }
        if settings.TASKS_INVOKER_SA:
            # OIDC token (harmless while the service allows unauthenticated; lets us
            # lock the endpoints down to this SA later without code changes).
            http_request["oidc_token"] = {"service_account_email": settings.TASKS_INVOKER_SA}
        task: dict = {"http_request": http_request}
        if name:
            task["name"] = client.task_path(settings.GCP_PROJECT, settings.TASKS_LOCATION,
                                            queue, name)
        if delay_seconds and delay_seconds > 0:
            import time
            ts = timestamp_pb2.Timestamp()
            ts.FromSeconds(int(time.time() + delay_seconds))
            task["schedule_time"] = ts
        try:
            client.create_task(request={"parent": parent, "task": task})
        except Exception as e:
            # A named task that already exists means it's already queued → dedup win.
            if type(e).__name__ == "AlreadyExists":
                print(f"[tasks] {name} already queued (dedup) — skipping")
                return True
            raise
        return True
    except Exception as e:  # pragma: no cover - infra failure path
        print(f"[tasks] enqueue to {queue}{path} failed ({type(e).__name__}: {e})")
        return False


def enqueue_process_fan(fan_id: str, user_uuid: str, delay_seconds: float) -> bool:
    """Schedule a /admin/process_fan POST for this fan ~delay_seconds from now.
    Returns False if debounce isn't wired up (caller falls back to sync reply)."""
    if not enabled():
        return False
    return _enqueue(settings.TASKS_QUEUE, "/admin/process_fan",
                    {"fan_id": fan_id, "user_uuid": user_uuid}, delay_seconds)


def welcome_enabled() -> bool:
    """True only if the proactive welcome is on and its queue is wired up."""
    return bool(settings.WELCOME_ENABLED and _can_enqueue(settings.WELCOME_TASKS_QUEUE))


def enqueue_welcome_fan(fan_id: str, user_uuid: str, delay_seconds: float) -> bool:
    """Schedule a /admin/welcome_fan POST for this brand-new follower ~delay_seconds
    from now. Deduped by a deterministic task name so a redelivered follow webhook
    can't queue two welcomes. Returns False if the welcome feature isn't wired up."""
    if not welcome_enabled():
        return False
    import re
    # Task names allow only [A-Za-z0-9_-]; fan uuids are fine, but a "sim:" prefix
    # (or any stray char) would be rejected — sanitize to be safe.
    name = "welcome-" + re.sub(r"[^A-Za-z0-9_-]", "-", fan_id)
    return _enqueue(settings.WELCOME_TASKS_QUEUE, "/admin/welcome_fan",
                    {"fan_id": fan_id, "user_uuid": user_uuid}, delay_seconds, name=name)


def enqueue_extract_memory(fan_id: str, incoming: str, final: str) -> bool:
    """Schedule mem0 fact-extraction for one turn onto the extract queue. Returns
    False if fan-out isn't wired up (caller runs it inline)."""
    if not fanout_enabled():
        return False
    return _enqueue(settings.EXTRACT_TASKS_QUEUE, "/admin/extract_memory",
                    {"fan_id": fan_id, "incoming": incoming, "final": final})


def enqueue_backfill_memory(fan_id: str) -> bool:
    """Schedule mem0 history backfill for one fan onto the extract queue. Returns
    False if fan-out isn't wired up (caller runs it inline)."""
    if not fanout_enabled():
        return False
    return _enqueue(settings.EXTRACT_TASKS_QUEUE, "/admin/backfill_memory",
                    {"fan_id": fan_id})
