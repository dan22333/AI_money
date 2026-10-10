"""Nightly reconcile — offline, Fanvue client mocked."""
import reconcile
import store


class FakeFanvue:
    def __init__(self, ready=True, subs=None, earnings=None):
        self.ready = ready
        self._subs = subs or []
        self._earn = earnings or []

    def list_subscribers(self):
        return self._subs

    def list_earnings(self, since=None):
        return self._earn


def _patch(monkeypatch, fake):
    monkeypatch.setattr(reconcile, "fanvue", fake)


def test_reconcile_skips_when_not_connected(monkeypatch):
    _patch(monkeypatch, FakeFanvue(ready=False))
    assert reconcile.run()["status"] == "skipped"


def test_reconcile_upserts_subscribers_and_earnings(monkeypatch):
    fake = FakeFanvue(
        subs=[{"uuid": "fan-1", "subscribedAt": "2026-10-01T00:00:00Z"},
              {"uuid": "fan-2"}],
        earnings=[{"fanUuid": "fan-1", "invoiceId": "INV-1", "grossCents": 1500,
                   "source": "message", "mediaUuids": ["m1"]}],
    )
    _patch(monkeypatch, fake)

    out = reconcile.run()
    assert out == {"status": "ok", "subscribersSynced": 2, "purchasesSynced": 1}

    f1 = store.get_fan("fan-1")
    assert f1["subscriptionStatus"] == "active"
    assert f1["subscribedAt"] == "2026-10-01T00:00:00Z"
    assert f1["totalSpendCents"] == 1500
    assert "m1" in f1["purchasedUuids"]
    assert store.get_fan("fan-2")["subscriptionStatus"] == "active"


def test_reconcile_is_idempotent(monkeypatch):
    fake = FakeFanvue(
        subs=[{"uuid": "fan-9"}],
        earnings=[{"fanUuid": "fan-9", "invoiceId": "INV-9", "grossCents": 2000,
                   "source": "message"}],
    )
    _patch(monkeypatch, fake)
    reconcile.run()
    reconcile.run()  # second pass must not double-count the invoice
    f = store.get_fan("fan-9")
    assert f["totalSpendCents"] == 2000
    assert f["purchaseCount"] == 1


def test_reconcile_skips_rows_missing_keys(monkeypatch):
    fake = FakeFanvue(
        subs=[{"nope": "x"}],
        earnings=[{"grossCents": 100}],  # no fan/invoice
    )
    _patch(monkeypatch, fake)
    assert reconcile.run() == {"status": "ok", "subscribersSynced": 0, "purchasesSynced": 0}
