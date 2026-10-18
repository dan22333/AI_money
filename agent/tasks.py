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


def enabled() -> bool:
    """True only if debounce is on and we can actually enqueue (queue + URL set)."""
    return bool(settings.DEBOUNCE_ENABLED and settings.TASKS_QUEUE
                and settings.SERVICE_BASE_URL and settings.USE_FIRESTORE)


def enqueue_process_fan(fan_id: str, user_uuid: str, delay_seconds: float) -> bool:
    """Schedule a /admin/process_fan POST for this fan ~delay_seconds from now.
    Returns True if enqueued, False if debounce isn't wired up (caller falls back).
    Never raises — an enqueue failure degrades to the synchronous path."""
    if not enabled():
        return False
    try:
        from google.cloud import tasks_v2
        from google.protobuf import timestamp_pb2

        client = tasks_v2.CloudTasksClient()
        parent = client.queue_path(settings.GCP_PROJECT, settings.TASKS_LOCATION,
                                   settings.TASKS_QUEUE)
        url = settings.SERVICE_BASE_URL.rstrip("/") + "/admin/process_fan"
        headers = {"Content-Type": "application/json"}
        if settings.SIM_SECRET:
            # the worker endpoint is protected by the same sim-secret as the other
            # /admin routes; attach it so the task is authorized when it fires.
            headers["x-sim-secret"] = settings.SIM_SECRET
        http_request: dict = {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": url,
            "headers": headers,
            "body": json.dumps({"fan_id": fan_id, "user_uuid": user_uuid}).encode(),
        }
        if settings.TASKS_INVOKER_SA:
            # OIDC token (harmless today since the service allows unauthenticated;
            # lets us lock the endpoint down to this SA later without code changes).
            http_request["oidc_token"] = {"service_account_email": settings.TASKS_INVOKER_SA}
        task: dict = {"http_request": http_request}
        if delay_seconds and delay_seconds > 0:
            import time
            ts = timestamp_pb2.Timestamp()
            ts.FromSeconds(int(time.time() + delay_seconds))
            task["schedule_time"] = ts
        # No task name (dedup id): each message legitimately needs its own settle
        # timer, and extra tasks are harmless — a task that fires after the batch
        # is already answered finds nothing pending and no-ops. See docs/debounce.
        client.create_task(request={"parent": parent, "task": task})
        return True
    except Exception as e:  # pragma: no cover - infra failure path
        print(f"[tasks] enqueue failed ({type(e).__name__}: {e}) — falling back to sync")
        return False
