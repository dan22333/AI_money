"""Reconcile / backfill — offline, Fanvue client mocked.

Covers the full backfill the reconcile now does: fans (followers + subscribers
merged by uuid), purchases (earnings, gross>0, idempotent by transactionOrderId),
message history (both directions, idempotent by messageUuid) and the media
catalog. Delta watermarks make re-runs safe without double-counting.
"""
import reconcile
import store


class FakeFanvue:
    def __init__(self, ready=True, me=None, followers=None, subs=None,
                 earnings=None, chats=None, chat_messages=None, media=None):
        self.ready = ready
        self._me = me or {"uuid": "creator-0"}
        self._followers = followers or []
        self._subs = subs or []
        self._earn = earnings or []
        self._chats = chats or []
        self._chat_messages = chat_messages or {}  # uuid -> [messages]

    def get_me(self):
        return self._me

    def list_followers(self):
        return self._followers

    def list_subscribers(self):
        return self._subs

    def list_earnings(self, since=None):
        # `since` is a delta watermark; our fakes just return everything and
        # rely on add_purchase's invoice idempotency, which is the real guard.
        return self._earn

    def list_chats(self, unread_only=True):
        return self._chats

    def list_chat_messages(self, user_uuid, since=None):
        return self._chat_messages.get(user_uuid, [])

    def list_media(self):
        return self._media if hasattr(self, "_media") and self._media else []


def _patch(monkeypatch, fake):
    monkeypatch.setattr(reconcile, "fanvue", fake)


def test_reconcile_skips_when_not_connected(monkeypatch):
    _patch(monkeypatch, FakeFanvue(ready=False))
    assert reconcile.run()["status"] == "skipped"


def test_reconcile_backfills_fans_and_purchases(monkeypatch):
    fake = FakeFanvue(
        followers=[{"uuid": "fan-1", "handle": "alice", "displayName": "Alice",
                    "registeredAt": "2026-10-01T00:00:00Z"},
                   {"uuid": "fan-2"}],
        subs=[{"uuid": "fan-1", "handle": "alice", "subscribedAt": "2026-10-02T00:00:00Z"}],
        earnings=[{"user": {"uuid": "fan-1"}, "transactionOrderId": "INV-1",
                   "gross": 1500, "date": "2026-10-03T00:00:00Z", "source": "message"}],
    )
    _patch(monkeypatch, fake)

    out = reconcile.run()
    assert out["status"] == "ok"
    assert out["fansSynced"] == 3        # 2 followers + 1 subscriber row processed
    assert out["purchasesSynced"] == 1

    # fan-1 is both a follower AND a subscriber → ONE merged row with both flags
    f1 = store.get_fan("fan-1")
    assert f1["isFollower"] is True
    assert f1["subscriptionStatus"] == "active"
    assert f1["handle"] == "alice"
    assert f1["totalSpendCents"] == 1500
    # fan-2 is only a follower
    f2 = store.get_fan("fan-2")
    assert f2["isFollower"] is True
    assert f2.get("subscriptionStatus") != "active"


def test_reconcile_downgrades_unfollow_and_unsubscribe(monkeypatch):
    """A fan present last run but ABSENT from the live list this run has
    unfollowed / unsubscribed — flags must be cleared, not left stale."""
    # run 1: fan-1 is both follower and subscriber
    _patch(monkeypatch, FakeFanvue(
        followers=[{"uuid": "fan-1"}],
        subs=[{"uuid": "fan-1"}],
    ))
    reconcile.run()
    f = store.get_fan("fan-1")
    assert f["isFollower"] is True and f["subscriptionStatus"] == "active"

    # run 2: fan-1 has dropped off BOTH lists → downgrade
    _patch(monkeypatch, FakeFanvue(followers=[], subs=[]))
    out = reconcile.run()
    assert out["fansDowngraded"] == 1
    f = store.get_fan("fan-1")
    assert f["isFollower"] is False
    assert f["subscriptionStatus"] == "expired"
    assert "unfollowedAt" in f and "unsubscribedAt" in f


def test_reconcile_skips_zero_dollar_earnings(monkeypatch):
    """$0 rows (free follows / free subs) are not spend and must not tier a fan."""
    fake = FakeFanvue(
        earnings=[{"user": {"uuid": "fan-7"}, "transactionOrderId": "FREE-1",
                   "gross": 0, "date": "2026-10-03T00:00:00Z", "source": "follow"}],
    )
    _patch(monkeypatch, fake)
    out = reconcile.run()
    assert out["purchasesSynced"] == 0
    assert store.get_fan("fan-7").get("totalSpendCents", 0) == 0


def test_reconcile_backfills_message_history(monkeypatch):
    fake = FakeFanvue(
        me={"uuid": "creator-0"},
        chats=[{"user": {"uuid": "fan-3"}}],
        chat_messages={"fan-3": [
            {"uuid": "m1", "text": "hey", "sender": {"uuid": "fan-3"},
             "sentAt": "2026-10-05T10:00:00Z"},
            {"uuid": "m2", "text": "hi babe", "sender": {"uuid": "creator-0"},
             "sentAt": "2026-10-05T10:01:00Z"},
        ]},
    )
    _patch(monkeypatch, fake)

    out = reconcile.run()
    assert out["messagesSynced"] == 2

    store.use_namespace_for("fan-3")
    msgs = store.recent_messages("fan-3", limit=10)
    by_uuid = {m["messageUuid"]: m for m in msgs}
    assert by_uuid["m1"]["role"] == "fan"
    assert by_uuid["m2"]["role"] == "jenny"   # creator == jenny
    # real sent time is preserved so it interleaves with live webhook messages
    assert by_uuid["m1"]["ts"] == "2026-10-05T10:00:00Z"


def test_reconcile_backfills_media_catalog(monkeypatch):
    fake = FakeFanvue()
    fake._media = [{"uuid": "media-1", "description": "beach set", "tags": ["beach"],
                    "recommendedPrice": 1200, "status": "ready"}]
    _patch(monkeypatch, fake)

    out = reconcile.run()
    assert out["mediaSynced"] == 1
    store.use_namespace_for("catalog")
    item = store._catalog_item("media-1")
    assert item["description"] == "beach set"
    assert item["suggestedPriceCents"] == 1200
    assert item["status"] == "ready"


def test_reconcile_is_idempotent(monkeypatch):
    fake = FakeFanvue(
        followers=[{"uuid": "fan-9"}],
        earnings=[{"user": {"uuid": "fan-9"}, "transactionOrderId": "INV-9",
                   "gross": 2000, "date": "2026-10-04T00:00:00Z", "source": "message"}],
        chats=[{"user": {"uuid": "fan-9"}}],
        chat_messages={"fan-9": [
            {"uuid": "m9", "text": "yo", "sender": {"uuid": "fan-9"},
             "sentAt": "2026-10-06T00:00:00Z"}]},
    )
    _patch(monkeypatch, fake)
    reconcile.run()
    reconcile.run()  # second pass must not double-count anything
    f = store.get_fan("fan-9")
    assert f["totalSpendCents"] == 2000
    assert f["purchaseCount"] == 1
    store.use_namespace_for("fan-9")
    assert len(store.recent_messages("fan-9", limit=10)) == 1


def test_reconcile_skips_rows_missing_keys(monkeypatch):
    fake = FakeFanvue(
        followers=[{"nope": "x"}],            # no uuid
        earnings=[{"gross": 100}],            # no user/invoice
    )
    _patch(monkeypatch, fake)
    out = reconcile.run()
    assert out["status"] == "ok"
    assert out["fansSynced"] == 0
    assert out["purchasesSynced"] == 0
