import graph
import router
import store


def test_message_event_runs_agent(monkeypatch):
    monkeypatch.setattr(graph, "handle_message",
                        lambda **k: {"reply": "hey!", "mode": "WARM", "actions": []})
    ev = {"type": "creator.message.received", "id": "e1",
          "data": {"sender": {"uuid": "sim:f"}, "message": {"text": "hi"}}}
    out = router.handle_event(ev, dry_run=True)
    assert out["status"] == "replied" and out["reply"] == "hey!"


def test_payment_records_and_reacts(monkeypatch):
    calls = {}

    def fake_handle(**k):
        calls["reacted"] = True
        return {"reply": "ty 😏", "actions": []}

    monkeypatch.setattr(graph, "handle_message", fake_handle)
    ev = {"type": "creator.payment.succeeded", "id": "p1",
          "data": {"buyer": {"uuid": "sim:f"}, "invoiceId": "FV-1",
                   "grossCents": 1500, "source": "message", "mediaUuids": ["m1"]}}
    out = router.handle_event(ev, dry_run=True)
    assert out["status"] == "purchase+reacted"
    assert store.get_fan("sim:f")["totalSpendCents"] == 1500
    assert "m1" in store.get_fan("sim:f")["purchasedUuids"]
    assert calls.get("reacted") is True


def test_duplicate_event_ignored(monkeypatch):
    monkeypatch.setattr(graph, "handle_message", lambda **k: {"reply": "x", "actions": []})
    ev = {"type": "creator.message.received", "id": "dup1",
          "data": {"sender": {"uuid": "sim:f"}, "message": {"text": "hi"}}}
    assert router.handle_event(ev, dry_run=True)["status"] == "replied"
    assert router.handle_event(ev, dry_run=True)["status"] == "duplicate"


def test_subscription_activated_marks_fan():
    ev = {"type": "creator.subscription.activated", "id": "s1",
          "data": {"subscriber": {"uuid": "sim:newfan"}}}
    out = router.handle_event(ev)
    assert out["status"] == "subscriber_added"
    assert store.get_fan("sim:newfan")["subscriptionStatus"] == "active"
