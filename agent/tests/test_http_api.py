"""FastAPI wiring tests — the HTTP layer (signature verify + auth guards) that
the router/graph unit tests don't exercise. Uses TestClient; all handlers mocked
below the HTTP boundary so nothing hits the network."""
import hashlib
import hmac
import time

from fastapi.testclient import TestClient

import graph
import main
import router
from config import settings

client = TestClient(main.app)


def test_health_ok():
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["ok"] is True


def _fanvue_sig(secret: bytes, body: bytes, ts: str, extra_macs=()) -> str:
    """Build an X-Fanvue-Signature header: t=<ts>,v0=<hmac of "ts.body">."""
    mac = hmac.new(secret, f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    macs = ",".join(f"v0={m}" for m in (mac, *extra_macs))
    return f"t={ts},{macs}"


def test_webhook_rejects_bad_signature(monkeypatch):
    monkeypatch.setattr(settings, "FANVUE_WEBHOOK_SECRET", "shh")
    # plain hex with no t=/v0= structure (the OLD, wrong scheme) must be rejected
    r = client.post("/webhook/fanvue", json={"type": "x"},
                    headers={"x-fanvue-signature": "deadbeef"})
    assert r.status_code == 401
    # right structure, wrong key → rejected
    body = b'{"type":"ping"}'
    bad = _fanvue_sig(b"wrong-key", body, str(int(time.time())))
    r = client.post("/webhook/fanvue", content=body,
                    headers={"x-fanvue-signature": bad, "content-type": "application/json"})
    assert r.status_code == 401


def test_webhook_rejects_stale_timestamp(monkeypatch):
    monkeypatch.setattr(settings, "FANVUE_WEBHOOK_SECRET", "shh")
    body = b'{"type":"ping"}'
    stale = _fanvue_sig(b"shh", body, str(int(time.time()) - 3600))  # 1h old → replay
    r = client.post("/webhook/fanvue", content=body,
                    headers={"x-fanvue-signature": stale, "content-type": "application/json"})
    assert r.status_code == 401


def test_webhook_accepts_valid_signature(monkeypatch):
    monkeypatch.setattr(settings, "FANVUE_WEBHOOK_SECRET", "shh")
    seen = {}
    monkeypatch.setattr(router, "handle_event",
                        lambda ev, dry_run=False: seen.update(ev) or {"status": "ok"})
    body = b'{"type":"ping","id":"e1"}'
    ts = str(int(time.time()))
    # include a second (bogus) v0 to exercise rotation: accept if ANY matches
    sig = _fanvue_sig(b"shh", body, ts, extra_macs=("00deadbeef",))
    r = client.post("/webhook/fanvue", content=body,
                    headers={"x-fanvue-signature": sig, "content-type": "application/json"})
    assert r.status_code == 200
    assert seen["type"] == "ping"  # raw body reached the router


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
    # isolate from ambient Fanvue state (a local token file would make the real
    # reconcile hit the network) — we only assert the auth guard here.
    import reconcile
    monkeypatch.setattr(reconcile, "run", lambda *a, **k: {"status": "ok"})
    assert client.post("/admin/reconcile").status_code == 401
    assert client.post("/admin/reconcile",
                       headers={"x-sim-secret": "topsecret"}).status_code == 200


def test_admin_fan_thread_auth_and_shape(monkeypatch):
    monkeypatch.setattr(settings, "SIM_SECRET", "topsecret")
    import store
    store.use_namespace_for("sim:threadtest")
    store.save_message("sim:threadtest", "fan", "hi jenny")
    store.save_message("sim:threadtest", "jenny", "hey you 😏")

    assert client.get("/admin/fan_thread?fan=sim:threadtest").status_code == 401
    r = client.get("/admin/fan_thread?fan=sim:threadtest",
                   headers={"x-sim-secret": "topsecret"})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert [m["role"] for m in body["messages"]] == ["fan", "jenny"]
