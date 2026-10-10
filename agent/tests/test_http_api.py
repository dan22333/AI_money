"""FastAPI wiring tests — the HTTP layer (signature verify + auth guards) that
the router/graph unit tests don't exercise. Uses TestClient; all handlers mocked
below the HTTP boundary so nothing hits the network."""
import hashlib
import hmac

from fastapi.testclient import TestClient

import graph
import main
import router
from config import settings

client = TestClient(main.app)


def test_health_ok():
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_webhook_rejects_bad_signature(monkeypatch):
    monkeypatch.setattr(settings, "FANVUE_WEBHOOK_SECRET", "shh")
    r = client.post("/webhook/fanvue", json={"type": "x"},
                    headers={"webhook-signature": "deadbeef"})
    assert r.status_code == 401


def test_webhook_accepts_valid_signature(monkeypatch):
    monkeypatch.setattr(settings, "FANVUE_WEBHOOK_SECRET", "shh")
    seen = {}
    monkeypatch.setattr(router, "handle_event",
                        lambda ev, dry_run=False: seen.update(ev) or {"status": "ok"})
    body = b'{"type":"ping","id":"e1"}'
    sig = hmac.new(b"shh", body, hashlib.sha256).hexdigest()
    r = client.post("/webhook/fanvue", content=body,
                    headers={"webhook-signature": sig, "content-type": "application/json"})
    assert r.status_code == 200
    assert seen["type"] == "ping"  # body reached the router


def test_webhook_open_when_no_secret_configured(monkeypatch):
    monkeypatch.setattr(settings, "FANVUE_WEBHOOK_SECRET", "")  # dev mode
    monkeypatch.setattr(router, "handle_event", lambda ev, dry_run=False: {"status": "ok"})
    r = client.post("/webhook/fanvue", json={"type": "ping"})
    assert r.status_code == 200


def test_simulate_requires_sim_secret(monkeypatch):
    monkeypatch.setattr(settings, "SIM_SECRET", "topsecret")
    assert client.post("/simulate", json={"text": "hi"}).status_code == 401
    assert client.post("/simulate", json={"text": "hi"},
                       headers={"x-sim-secret": "wrong"}).status_code == 401


def test_simulate_runs_and_forces_sim_prefix(monkeypatch):
    monkeypatch.setattr(settings, "SIM_SECRET", "topsecret")
    seen = {}

    def fake(**kw):
        seen.update(kw)
        return {"reply": "hey 💕", "mode": "WARM", "actions": []}

    monkeypatch.setattr(graph, "handle_message", fake)
    r = client.post("/simulate", json={"fan_id": "mike", "text": "yo"},
                    headers={"x-sim-secret": "topsecret"})
    assert r.status_code == 200 and r.json()["reply"] == "hey 💕"
    assert seen["fan_id"].startswith("sim:")  # non-sim id is coerced to sim namespace


def test_admin_reconcile_auth_guard(monkeypatch):
    monkeypatch.setattr(settings, "SIM_SECRET", "topsecret")
    assert client.post("/admin/reconcile").status_code == 401
    # correct secret → authorized (body shape varies by branch; only assert auth)
    assert client.post("/admin/reconcile",
                       headers={"x-sim-secret": "topsecret"}).status_code == 200
