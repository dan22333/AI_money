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


@app.post("/admin/reconcile")
def reconcile(x_sim_secret: str | None = Header(default=None)):
    if settings.SIM_SECRET and x_sim_secret != settings.SIM_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")
    import reconcile as _reconcile
    return _reconcile.run()


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
