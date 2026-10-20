"""FastAPI service — the always-on Cloud Run container.

/webhook/fanvue  — Fanvue pushes events here (verified, routed)
/simulate        — test the agent with a synthetic fan (no Fanvue); sim-secret protected
/admin/reconcile — called by Cloud Scheduler to backfill/repair from the API
/health          — liveness (NOT /healthz: Google Front End reserves /healthz
                   on *.run.app and returns its own 404 before the request ever
                   reaches the container)
"""
from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

import router
from config import settings

app = FastAPI(title="Jenny Agent", version="1.0.0")


@app.get("/health")
def health():
    return {"ok": True, "voice": settings.MODEL, "mode_model": settings.MODE_MODEL}


def _signing_keys() -> list[bytes]:
    """Plausible HMAC key encodings for the configured signing secret.

    Fanvue's secret is `whsec_` + 64 hex chars (the user may store it with or
    without the prefix). Webhook implementations disagree on what bytes to key
    the HMAC with — the raw string, the hex-decoded 32 bytes, or the prefixed
    string — so we try each and accept if any produces a matching MAC. Avoids a
    guess-and-redeploy loop on the exact convention.
    """
    secret = settings.FANVUE_WEBHOOK_SECRET
    hexpart = secret[len("whsec_"):] if secret.startswith("whsec_") else secret
    candidates = [secret.encode(), hexpart.encode(), f"whsec_{hexpart}".encode()]
    try:
        candidates.append(bytes.fromhex(hexpart))  # 64 hex → 32 raw key bytes
    except ValueError:
        pass
    out: list[bytes] = []
    for k in candidates:
        if k not in out:
            out.append(k)
    return out


def _verify(body: bytes, sig_header: str | None) -> bool:
    """Verify a Fanvue webhook signature.

    Fanvue sends `X-Fanvue-Signature: t=<unix_ts>,v0=<hex>[,v0=<hex>...]`, where
    each MAC is HMAC-SHA256(key, f"{t}.{raw_body}"). Several v0 values can be
    present during signing-secret rotation — accept if ANY matches. The RAW body
    bytes must be used exactly as received (re-serialized JSON would fail). Reject
    if the timestamp skews more than 300s (replay protection).
    """
    if not settings.FANVUE_WEBHOOK_SECRET:
        return True  # not enforced in dev
    if not sig_header:
        return False
    ts: str | None = None
    macs: list[str] = []
    for part in sig_header.split(","):
        key, _, val = part.strip().partition("=")
        if key == "t":
            ts = val
        elif key == "v0":
            macs.append(val)
    if not ts or not macs:
        return False
    try:
        if abs(time.time() - int(ts)) > 300:
            return False
    except ValueError:
        return False
    signed = f"{ts}.".encode() + body
    for key in _signing_keys():
        expected = hmac.new(key, signed, hashlib.sha256).hexdigest()
        if any(hmac.compare_digest(expected, m) for m in macs):
            return True
    return False


@app.post("/webhook/fanvue")
async def fanvue_webhook(request: Request):
    body = await request.body()
    sig = request.headers.get("x-fanvue-signature") or request.headers.get("webhook-signature")
    if not _verify(body, sig):
        raise HTTPException(status_code=401, detail="bad signature")
    event = await request.json()
    return router.handle_event(event, dry_run=False)


class SimulateIn(BaseModel):
    fan_id: str = "sim:demo-fan"
    text: str
    dry_run: bool = True


@app.post("/simulate")
def simulate(inp: SimulateIn, x_sim_secret: str | None = Header(default=None)):
    """Synthetic-fan test. Requires sim secret if one is configured. Always sim: namespaced."""
    if settings.SIM_SECRET and x_sim_secret != settings.SIM_SECRET:
        raise HTTPException(status_code=401, detail="bad sim secret")
    fan_id = inp.fan_id if inp.fan_id.startswith("sim:") else f"sim:{inp.fan_id}"
    import graph
    return graph.handle_message(fan_id=fan_id, user_uuid=fan_id, text=inp.text, dry_run=inp.dry_run)


class ProcessFanIn(BaseModel):
    fan_id: str
    user_uuid: str = ""


@app.post("/admin/process_fan")
def process_fan(inp: ProcessFanIn, request: Request,
                x_sim_secret: str | None = Header(default=None)):
    """Cloud Tasks worker for the per-fan debounce (DEBOUNCE_ENABLED). Protected by
    the same sim-secret as the other /admin routes (the enqueue attaches it). The
    `X-CloudTasks-TaskRetryCount` header tells us this is a redelivery so we can do
    the Fanvue read-back before resending. Returns 2xx only after the reply is sent
    + recorded; any error → non-2xx so Cloud Tasks redelivers with backoff."""
    if settings.SIM_SECRET and x_sim_secret != settings.SIM_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")
    import debounce
    retry_count = request.headers.get("x-cloudtasks-taskretrycount", "0")
    try:
        is_retry = int(retry_count) > 0
    except ValueError:
        is_retry = False
    return debounce.process_fan(inp.fan_id, inp.user_uuid or inp.fan_id, is_retry=is_retry)


class ExtractMemoryIn(BaseModel):
    fan_id: str
    incoming: str = ""
    final: str = ""


@app.post("/admin/extract_memory")
def extract_memory(inp: ExtractMemoryIn, x_sim_secret: str | None = Header(default=None)):
    """Cloud Tasks worker for extraction fan-out (MEMORY_FANOUT_ENABLED): runs the
    mem0 fact-extraction + summary for one turn off the reply path. Idempotent
    enough to retry (mem0 de-dupes facts). sim-secret protected."""
    if settings.SIM_SECRET and x_sim_secret != settings.SIM_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")
    import graph
    import store
    store.use_namespace_for(inp.fan_id)
    graph.learn_from_turn(inp.fan_id, inp.incoming, inp.final)
    return {"status": "learned", "fan": inp.fan_id}


class BackfillMemoryIn(BaseModel):
    fan_id: str


@app.post("/admin/backfill_memory")
def backfill_memory(inp: BackfillMemoryIn, x_sim_secret: str | None = Header(default=None)):
    """Cloud Tasks worker for extraction fan-out: backfills one fan's stored history
    into mem0 (watermarked, resumable). Reconcile enqueues one of these per fan so
    the backfill parallelizes instead of running sequentially in one request."""
    if settings.SIM_SECRET and x_sim_secret != settings.SIM_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")
    import reconcile as _reconcile
    import store
    store.use_namespace_for(inp.fan_id)
    extracted = _reconcile._backfill_memory(inp.fan_id)
    return {"status": "backfilled", "fan": inp.fan_id, "extracted": extracted}


class RecallEvalIn(BaseModel):
    fan_id: str
    message: str
    turns: int = 0  # recent stored turns to include as context (0 → RECALL_CONTEXT_TURNS)


@app.post("/admin/recall_eval")
def recall_eval(inp: RecallEvalIn, x_sim_secret: str | None = Header(default=None)):
    """Read-only: run all three recall strategies (latest / context / expanded) for
    one fan + probe message against the REAL memory store, and return what each
    retrieves (+ the expanded probes). Lets us judge recall quality before flipping
    RECALL_STRATEGY. No writes. sim-secret protected; namespaced by the fan id."""
    if settings.SIM_SECRET and x_sim_secret != settings.SIM_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")
    import memory
    import store
    store.use_namespace_for(inp.fan_id)
    n = inp.turns or settings.RECALL_CONTEXT_TURNS
    hist = store.recent_messages(inp.fan_id, limit=n)
    recent_turns = [{"role": m.get("role"), "text": m.get("text")} for m in hist]
    return {"fan": inp.fan_id, "strategyInUse": settings.RECALL_STRATEGY,
            **memory.recall_eval(inp.fan_id, inp.message, recent_turns)}


@app.post("/admin/refresh_tokens")
def refresh_tokens(x_sim_secret: str | None = Header(default=None)):
    """Proactively refresh Fanvue OAuth tokens that are near expiry. Driven by the
    Cloud Scheduler cron (the 'single writer') so tokens never idle out or expire
    mid-conversation; on-demand refresh stays as a fallback. Lock-guarded + idempotent
    — safe on every tick. sim-secret protected."""
    if settings.SIM_SECRET and x_sim_secret != settings.SIM_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")
    import fanvue_client
    return {"status": "ok", **fanvue_client.refresh_all()}


@app.post("/admin/reconcile")
def reconcile(dry_run: bool = False, full_earnings: bool = False,
              x_sim_secret: str | None = Header(default=None)):
    """Pull from Fanvue and upsert idempotently into Firestore/mem0. Pass
    ?dry_run=true to PREVIEW: pulls + returns would-change counts but writes
    nothing — a safety rail to sanity-check reach/connectivity before a real run.

    ?full_earnings=true ignores the earnings delta watermark and re-reads ALL
    earnings — a one-time backfill that patches net (creator take-home) + currency
    onto purchases recorded before net was tracked. Idempotent (gross isn't
    re-counted); the watermark still advances so nightly runs stay incremental."""
    if settings.SIM_SECRET and x_sim_secret != settings.SIM_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")
    import reconcile as _reconcile
    return _reconcile.run(dry_run=dry_run, full_earnings=full_earnings)


@app.post("/admin/dedup_messages")
def dedup_messages(apply: bool = False, x_sim_secret: str | None = Header(default=None)):
    """One-time cleanup of the pre-#3 double-write: live webhook messages used to be
    stored under a random doc id with no messageUuid, then the reconcile re-pull
    stored a SECOND copy keyed by messageUuid. This sweeps the PROD namespace and
    drops each random-id copy that now has a uuid-keyed twin (same role+text),
    keeping the canonical uuid-keyed doc. Dry-run by default — pass ?apply=true to
    delete. Safe to run repeatedly (idempotent)."""
    if settings.SIM_SECRET and x_sim_secret != settings.SIM_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")
    import store
    store.use_namespace_for("__reconcile__")  # select the prod (non-sim) namespace
    per_fan = []
    totals = {"examined": 0, "duplicates": 0, "deleted": 0}
    for fan in store.list_fans():
        uuid = fan.get("fanUuid")
        if not uuid or str(uuid).startswith("sim:"):
            continue
        r = store.dedup_live_messages(uuid, apply=apply)
        if r["duplicates"]:
            per_fan.append(r)
        for k in totals:
            totals[k] += r[k]
    return {"apply": apply, "fans_with_dupes": len(per_fan), **totals, "detail": per_fan}


@app.get("/admin/fan_thread")
def fan_thread(fan: str, limit: int = 200, x_sim_secret: str | None = Header(default=None)):
    """Read-only: dump one fan's stored profile + full message thread (backfilled
    history + live webhook turns interleaved by ts). Ops visibility for a specific
    conversation. sim-secret protected; namespaced by the fan id."""
    if settings.SIM_SECRET and x_sim_secret != settings.SIM_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")
    import store
    store.use_namespace_for(fan)
    f = store.get_fan(fan)
    msgs = store.recent_messages(fan, limit=limit)
    keep = ("fanUuid", "handle", "displayName", "subscriptionStatus", "isFollower",
            "totalSpendCents", "tier", "purchaseCount", "statusEvents",
            "subscribedAt", "unsubscribedAt", "followedAt", "unfollowedAt")
    return {"fan": {k: f.get(k) for k in keep if k in f},
            "count": len(msgs),
            "messages": [{"role": m.get("role"), "text": m.get("text"), "ts": m.get("ts"),
                          "source": m.get("source")} for m in msgs]}


@app.post("/admin/memcheck")
def memcheck(x_sim_secret: str | None = Header(default=None)):
    """Real memory write+read roundtrip in the STAGING collection. The canary
    calls this and fails promotion if memory is enabled but broken. 503 on error
    so a bad memory layer never reaches production traffic."""
    if settings.SIM_SECRET and x_sim_secret != settings.SIM_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")
    import memory
    import store
    store.use_namespace_for("sim:__memcheck__")  # staging collection only
    try:
        result = memory.selftest()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"memory selftest raised: {e}")
    if result.get("status") == "error":
        raise HTTPException(status_code=503, detail=result.get("detail", "memory selftest failed"))
    return result


class ProbeIn(BaseModel):
    paths: list[str] = []
    post_path: str = ""      # if set, do a POST to discover the send endpoint
    post_body: dict = {}


@app.post("/admin/fanvue_probe")
def fanvue_probe(inp: ProbeIn | None = None, x_sim_secret: str | None = Header(default=None)):
    """Diagnostic: GET curated (or supplied) Fanvue endpoints with the server's
    own token and report status + top-level keys + a sample item, so we can
    discover real list/chat shapes (send path, followers, earnings) without
    desyncing the rotating refresh token by probing from a laptop. sim-secret
    protected. Temporary — remove once the shapes are pinned."""
    if settings.SIM_SECRET and x_sim_secret != settings.SIM_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")
    from fanvue_client import fanvue
    if not fanvue.ready:
        return {"ready": False}
    default_paths = ["/v1/users/me", "/v1/chats", "/v1/followers", "/v1/subscribers",
                     "/v1/insights/subscribers", "/v1/insights/earnings"]
    paths = inp.paths if (inp and inp.paths) else default_paths
    report: dict = {}
    for p in paths:
        if not p.startswith("/v"):
            report[p] = {"error": "path must start with /v"}
            continue
        try:
            status, body = fanvue.raw_get(p)
            entry: dict = {"status": status}
            if isinstance(body, dict):
                entry["keys"] = list(body.keys())[:20]
                data = body.get("data")
                if isinstance(data, list):
                    entry["count"] = len(data)
                    if data and isinstance(data[0], dict):
                        entry["item_keys"] = list(data[0].keys())[:30]
                        entry["item_sample"] = {k: str(v)[:80] for k, v in list(data[0].items())[:30]}
                else:
                    entry["sample"] = {k: str(v)[:80] for k, v in list(body.items())[:20]}
            else:
                entry["body"] = str(body)[:300]
            report[p] = entry
        except Exception as e:
            report[p] = {"error": f"{type(e).__name__}: {e}"}
    if inp and inp.post_path and inp.post_path.startswith("/v"):
        try:
            status, body = fanvue.raw_post(inp.post_path, inp.post_body)
            report[f"POST {inp.post_path}"] = {"status": status, "body": str(body)[:300]}
        except Exception as e:
            report[f"POST {inp.post_path}"] = {"error": f"{type(e).__name__}: {e}"}
    return report
