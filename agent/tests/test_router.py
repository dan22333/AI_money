import graph
import router
import store
import tasks
from config import settings


def test_message_event_runs_agent(monkeypatch):
    monkeypatch.setattr(graph, "handle_message",
                        lambda **k: {"reply": "hey!", "mode": "WARM", "actions": []})
    ev = {"type": "creator.message.received", "id": "e1",
          "data": {"sender": {"uuid": "sim:f"}, "message": {"text": "hi"}}}
    out = router.handle_event(ev, dry_run=True)
    assert out["status"] == "replied" and out["reply"] == "hey!"


def test_legacy_flat_message_runs_agent(monkeypatch):
    """Real Fanvue legacy webhook: FLAT payload, NO `type` field — detect by shape
    (has message + sender/messageUuid). This is the actual live production shape."""
    monkeypatch.setattr(graph, "handle_message",
                        lambda **k: {"reply": "heyy 🙈", "mode": "WARM", "actions": []})
    ev = {"messageUuid": "m-1", "recipientUuid": "jenny", "eventId": "evt-1",
          "sender": {"uuid": "fan-77"}, "message": {"text": "hi jenny"},
          "unreadMessagesCount": 1, "isMuted": False}
    out = router.handle_event(ev, dry_run=True)
    assert out["status"] == "replied" and out["reply"] == "heyy 🙈"


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


def test_flat_follow_event_records_fan():
    """Real Fanvue follow webhook: FLAT, NO `type` field — {recipientUuid, sender,
    timestamp, eventId}, no `message`. This is the actual live shape (fan billiamj,
    2026-11-01) that used to fall through to 'ignored', losing real-time new fans."""
    ev = {"recipientUuid": "jenny", "sender": {"uuid": "sim:follower99"},
          "timestamp": "2026-11-01T15:43:10Z", "eventId": "evt-follow-1"}
    out = router.handle_event(ev, dry_run=True)
    assert out["status"] == "follower_added"
    assert out.get("inferred_flat") is True
    assert store.get_fan("sim:follower99")["isFollower"] is True


def test_flat_read_receipt_still_ignored():
    """A Fanvue 'read' event ({counterpartUuid, readMessagesCount, ...}, no sender)
    must NOT be mistaken for a follow — it stays ignored."""
    ev = {"counterpartUuid": "sim:somefan", "readMessagesCount": 2,
          "unreadMessagesCount": 0, "recipientUuid": "jenny",
          "timestamp": "2026-11-01T15:44:00Z", "eventId": "evt-read-1"}
    out = router.handle_event(ev, dry_run=True)
    assert out["status"] == "ignored"


def _spy_welcome(monkeypatch):
    calls = []
    monkeypatch.setattr(tasks, "enqueue_welcome_fan",
                        lambda fan_id, user_uuid, delay: calls.append(fan_id) or True)
    return calls


def test_new_follow_enqueues_welcome_when_enabled(monkeypatch):
    """A genuinely-new follow (flat live shape) schedules the welcome DM."""
    monkeypatch.setattr(settings, "WELCOME_ENABLED", True)
    calls = _spy_welcome(monkeypatch)
    ev = {"recipientUuid": "jenny", "sender": {"uuid": "sim:welcomee"},
          "timestamp": "2026-11-01T15:43:10Z", "eventId": "evt-wel-1"}
    out = router.handle_event(ev)  # real (not dry_run) → welcome path active
    assert out["status"] == "follower_added"
    assert calls == ["sim:welcomee"]


def test_welcome_not_enqueued_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "WELCOME_ENABLED", False)
    calls = _spy_welcome(monkeypatch)
    ev = {"recipientUuid": "jenny", "sender": {"uuid": "sim:welcomee2"},
          "timestamp": "2026-11-01T15:43:10Z", "eventId": "evt-wel-2"}
    router.handle_event(ev)
    assert calls == []


def test_welcome_not_enqueued_on_dry_run(monkeypatch):
    """Canary/sim traffic (dry_run) must never schedule a real welcome DM."""
    monkeypatch.setattr(settings, "WELCOME_ENABLED", True)
    calls = _spy_welcome(monkeypatch)
    ev = {"recipientUuid": "jenny", "sender": {"uuid": "sim:welcomee3"},
          "timestamp": "2026-11-01T15:43:10Z", "eventId": "evt-wel-3"}
    router.handle_event(ev, dry_run=True)
    assert calls == []


def test_welcome_not_re_enqueued_for_existing_follower(monkeypatch):
    """A re-delivered/again follow for someone already following is a no-op
    (mark_followed returns False) → no second welcome."""
    monkeypatch.setattr(settings, "WELCOME_ENABLED", True)
    f = store.get_fan("sim:already")
    store.mark_followed(f, source="reconcile")
    store.save_fan(f)
    calls = _spy_welcome(monkeypatch)
    ev = {"recipientUuid": "jenny", "sender": {"uuid": "sim:already"},
          "timestamp": "2026-11-01T15:43:10Z", "eventId": "evt-wel-4"}
    router.handle_event(ev)
    assert calls == []
