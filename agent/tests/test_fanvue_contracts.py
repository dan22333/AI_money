"""Fanvue contract tests — pin the parsing of (assumed) real payload shapes.

Loads the fixtures in fixtures/fanvue/ through the REAL router/client code so a
field-path change (or a real captured payload that differs) breaks a test. The
graph is mocked — we test parsing/routing, not the LLM.

See fixtures/fanvue/README.md for how to replace the assumed shapes with real
captured ones and thereby verify the contract.
"""
import json
from pathlib import Path

import fanvue_client
import graph
import router
import store

FIX = Path(__file__).parent / "fixtures" / "fanvue"


def _load(name):
    return json.loads((FIX / name).read_text())


def test_message_event_parses(monkeypatch):
    seen = {}
    monkeypatch.setattr(graph, "handle_message",
                        lambda **kw: seen.update(kw) or {"reply": "hi", "mode": "WARM", "actions": []})
    out = router.handle_event(_load("webhook_message.json"), dry_run=True)
    assert out["status"] == "replied"
    assert seen["fan_id"] == "fan-abc-123"           # sender.uuid extracted
    assert seen["text"].startswith("heyy just subscribed")


def test_payment_event_records_purchase(monkeypatch):
    monkeypatch.setattr(graph, "handle_message", lambda **kw: {"reply": "ty", "actions": []})
    out = router.handle_event(_load("webhook_payment.json"), dry_run=True)
    assert out["status"] == "purchase+reacted"
    fan = store.get_fan("fan-abc-123")
    assert fan["totalSpendCents"] == 1500            # grossCents parsed
    assert "media-xyz-1" in fan["purchasedUuids"]    # mediaUuids parsed


def test_subscription_event_marks_fan():
    out = router.handle_event(_load("webhook_subscription.json"))
    assert out["status"] == "subscriber_added"
    assert store.get_fan("fan-new-555")["subscriptionStatus"] == "active"


def test_follow_event_creates_fan():
    out = router.handle_event(_load("webhook_follow.json"))
    assert out["status"] == "follower_added"
    assert store.get_fan("fan-follow-777")["fanUuid"] == "fan-follow-777"


def test_send_message_body_contract(monkeypatch):
    """Pin the outbound send shape: {text, mediaUuids, price} + auth header + URL."""
    captured = {}

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": True}

    def fake_post(url, headers=None, json=None, timeout=None, **k):
        captured.update(url=url, headers=headers, json=json)
        return Resp()

    monkeypatch.setattr(fanvue_client.httpx, "post", fake_post)
    monkeypatch.setattr(fanvue_client.fanvue, "_access_token", lambda: "tok-123")

    fanvue_client.fanvue.send_message("user-9", "come see 😏", media_uuids=["m1"], price_cents=1500)
    assert captured["json"] == {"text": "come see 😏", "mediaUuids": ["m1"], "price": 1500}
    assert captured["url"].endswith("/v1/chats/user-9/message")
    assert captured["headers"]["Authorization"] == "Bearer tok-123"
