from datetime import datetime, timedelta, timezone

import store


def _seed():
    store.add_catalog_item("warm1", "beach teaser", ["beach"], heat=2, suggested_price_cents=0)
    store.add_catalog_item("flirty1", "lingerie tease", ["lingerie"], heat=3, suggested_price_cents=800)
    store.add_catalog_item("explicit1", "spicy bedroom set", ["spicy"], heat=4, suggested_price_cents=1500)


def test_named_lock_is_mutually_exclusive_and_self_heals():
    # free → first owner wins, a different live owner is refused
    assert store.acquire_lock("L", "owner-a", ttl_seconds=60) is True
    assert store.acquire_lock("L", "owner-b", ttl_seconds=60) is False
    # the holder can re-acquire (idempotent) and then release it
    assert store.acquire_lock("L", "owner-a", ttl_seconds=60) is True
    store.release_lock("L", "owner-a")
    assert store.acquire_lock("L", "owner-b", ttl_seconds=60) is True
    # a release by someone who no longer owns it is a no-op
    store.release_lock("L", "owner-a")
    assert store.acquire_lock("L", "owner-c", ttl_seconds=60) is False
    store.release_lock("L", "owner-b")


def test_named_lock_expires_so_a_dead_holder_cannot_wedge_it():
    assert store.acquire_lock("L2", "dead", ttl_seconds=-1) is True  # already expired
    # expired lease → a new owner may steal it
    assert store.acquire_lock("L2", "live", ttl_seconds=60) is True
    assert store.acquire_lock("L2", "other", ttl_seconds=60) is False
    store.release_lock("L2", "live")


def test_heat_gate_excludes_too_hot():
    _seed()
    warm = {i["mediaUuid"] for i in store.eligible_media("sim:f", "WARM")}
    assert warm == {"warm1"}  # heat 3 & 4 excluded in WARM

    explicit = {i["mediaUuid"] for i in store.eligible_media("sim:f", "EXPLICIT")}
    assert explicit == {"warm1", "flirty1", "explicit1"}


def test_purchased_items_are_never_eligible():
    _seed()
    store.add_purchase("sim:f", "inv1", gross_cents=1500, source="message",
                       media_uuids=["explicit1"])
    ids = {i["mediaUuid"] for i in store.eligible_media("sim:f", "EXPLICIT")}
    assert "explicit1" not in ids  # already bought → filtered
    assert "sim:f" in [k for k in [store.get_fan("sim:f")["fanUuid"]]]
    assert store.get_fan("sim:f")["totalSpendCents"] == 1500


def test_recently_offered_excluded():
    _seed()
    store.record_offer("sim:f", "flirty1")
    ids = {i["mediaUuid"] for i in store.eligible_media("sim:f", "EXPLICIT")}
    assert "flirty1" not in ids


def test_paid_only_filter():
    _seed()
    paid = {i["mediaUuid"] for i in store.eligible_media("sim:f", "EXPLICIT", paid_only=True)}
    assert paid == {"flirty1", "explicit1"}  # warm1 is free → excluded


def test_cooldown_by_offer_count(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "MAX_OFFERS_PER_SESSION", 1)
    assert store.cooldown_active("sim:f") is False
    store.record_offer("sim:f", "flirty1")
    assert store.cooldown_active("sim:f") is True  # hit the per-session cap


def test_sessions_reset_after_gap(monkeypatch):
    base = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(store, "_now", lambda: base)
    sid1, new1 = store.compute_session("sim:f")
    assert new1 is True
    # same minute → same session
    monkeypatch.setattr(store, "_now", lambda: base + timedelta(minutes=5))
    sid2, new2 = store.compute_session("sim:f")
    assert new2 is False and sid2 == sid1
    # long gap → new session
    monkeypatch.setattr(store, "_now", lambda: base + timedelta(hours=3))
    sid3, new3 = store.compute_session("sim:f")
    assert new3 is True and sid3 != sid1


def test_purchase_is_idempotent():
    store.add_purchase("sim:f", "inv1", gross_cents=500, source="tip")
    store.add_purchase("sim:f", "inv1", gross_cents=500, source="tip")
    assert store.get_fan("sim:f")["purchaseCount"] == 1


# ---------------- message dedup (#3) ----------------
def test_upsert_message_is_idempotent_by_uuid():
    store.use_namespace_for("fan-u")
    assert store.upsert_message("fan-u", "m1", "fan", "hey") is True
    assert store.upsert_message("fan-u", "m1", "fan", "hey again") is False  # same uuid → no-op
    assert store.message_count("fan-u") == 1


def test_dedup_live_messages_removes_shadowed_random_copies():
    store.use_namespace_for("fan-d")
    # pre-fix live write: random doc id, no messageUuid (both sides)
    store.save_message("fan-d", "fan", "hi jenny")
    store.save_message("fan-d", "jenny", "heyy babe")
    # reconcile re-pull: the SAME messages, now keyed by their Fanvue uuid
    store.upsert_message("fan-d", "u-fan", "fan", "hi jenny")
    store.upsert_message("fan-d", "u-jen", "jenny", "heyy babe")
    assert store.message_count("fan-d") == 4  # duplicated

    dry = store.dedup_live_messages("fan-d")
    assert dry["duplicates"] == 2 and dry["deleted"] == 0  # dry-run never deletes
    assert store.message_count("fan-d") == 4

    applied = store.dedup_live_messages("fan-d", apply=True)
    assert applied["deleted"] == 2
    assert store.message_count("fan-d") == 2  # only the canonical uuid-keyed docs remain
    assert all(m.get("messageUuid") for m in store.recent_messages("fan-d", limit=10))


def test_dedup_keeps_random_copies_without_a_uuid_twin():
    store.use_namespace_for("fan-s")
    store.save_message("fan-s", "jenny", "reply that reconcile never re-pulled")
    applied = store.dedup_live_messages("fan-s", apply=True)
    assert applied["deleted"] == 0 and store.message_count("fan-s") == 1
