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

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

import router
from config import settings

app = FastAPI(title="Jenny Agent", version="1.0.0")


@app.get("/health")
def health():
    return {"ok": True, "voice": settings.MODEL, "mode_model": settings.MODE_MODEL}


def _verify(body: bytes, signature: str | None) -> bool:
    if not settings.FANVUE_WEBHOOK_SECRET:
        return True  # not enforced in dev
    if not signature:
        return False
    expected = hmac.new(settings.FANVUE_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@app.post("/webhook/fanvue")
async def fanvue_webhook(request: Request):
    body = await request.body()
    sig = request.headers.get("webhook-signature") or request.headers.get("x-fanvue-signature")
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
    # TODO Phase 2: pull /earnings, /subscribers, /insights/fans and upsert.
    return {"status": "reconcile stub — wired in Phase 2"}
