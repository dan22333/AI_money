"""Selling via the menu + single send_media tool. The agent picks an id from the
deterministic 'YOU CAN SEND NOW' menu; the tool resolves it to real media + price,
gates paid sends, and records the offer. All offline (dry_run), no LLM."""
import store
import tools


def _menu(mode, fan="sim:f"):
    """Mirror graph.load_context: build the sendable menu and load it into the tool
    context, then return it so the test can look up ids."""
    tools.set_context(fan_id=fan, user_uuid=fan, dry_run=True)
    menu = store.sendable_menu(fan, mode)
    tools.update_context(mode=mode, menu=menu)
    return menu


def _id_for(menu, uuid):
    return next((e["id"] for e in menu.values() if uuid in e["media_uuids"]), None)


def _seed():
    store.add_catalog_item("ppv_flirty", "lingerie tease set", ["lingerie"], heat=3,
                           suggested_price_cents=800)
    store.add_catalog_item("ppv_explicit", "spicy bedroom set", ["spicy", "bedroom"], heat=4,
                           suggested_price_cents=1500)


def test_paid_items_absent_from_menu_when_mode_too_cool():
    _seed()
    menu = _menu("WARM")  # selling not open below FLIRTY
    assert all(e["price_cents"] == 0 for e in menu.values())


def test_sends_eligible_paid_item_and_records():
    _seed()
    menu = _menu("EXPLICIT")
    mid = _id_for(menu, "ppv_explicit")
    assert mid
    res = tools.send_media.invoke({"media_id": mid, "caption": "just for you 😏"})
    assert res.startswith("sent::") and "$15.00" in res
    out = tools.get_outbox()[-1]
    assert out["type"] == "ppv" and out["mediaUuids"] == ["ppv_explicit"]
    assert out["price_cents"] == 1500
    fan = store.get_fan("sim:f")
    assert "ppv_explicit" in fan["recentlyOfferedUuids"]           # not re-offered
    assert any(o["mediaUuid"] == "ppv_explicit" for o in fan["pendingOffers"])  # locked until paid


def test_purchased_item_never_in_menu():
    store.add_catalog_item("ppv_only", "spicy bedroom set", ["spicy", "bedroom"], heat=4,
                           suggested_price_cents=1500)
    store.add_purchase("sim:f", "inv1", gross_cents=1500, source="message",
                       media_uuids=["ppv_only"])
    menu = _menu("EXPLICIT")
    assert _id_for(menu, "ppv_only") is None


def test_unknown_or_hallucinated_id_sends_nothing():
    _seed()
    _menu("EXPLICIT")
    assert tools.send_media.invoke({"media_id": "m99", "caption": "x"}) == "unavailable"


def test_cooldown_blocks_second_paid_send(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "MAX_OFFERS_PER_SESSION", 1)
    _seed()
    menu = _menu("EXPLICIT")
    first = tools.send_media.invoke({"media_id": _id_for(menu, "ppv_explicit"), "caption": "a"})
    assert first.startswith("sent::")
    # menu was built before the send; the 2nd paid send is caught by the cooldown re-check
    second = tools.send_media.invoke({"media_id": _id_for(menu, "ppv_flirty"), "caption": "b"})
    assert second == "unavailable"


def test_free_teaser_sends_and_does_not_burn_ppv_cooldown():
    store.add_catalog_item("free1", "gym selfie", ["gym"], heat=2, suggested_price_cents=0)
    store.add_catalog_item("paid1", "paid set", ["gym"], heat=3, suggested_price_cents=900)
    menu = _menu("WARM")  # WARM: only the free teaser is sendable
    mid = _id_for(menu, "free1")
    assert mid
    res = tools.send_media.invoke({"media_id": mid, "caption": "hi"})
    assert res == "sent::gym selfie"
    assert tools.get_outbox()[-1]["mediaUuids"] == ["free1"]
    # a FREE teaser must NOT consume the paid-offer budget (that was the old bug)
    assert store.get_fan("sim:f").get("offersThisSession", 0) == 0


def test_recently_offered_resets_next_session_but_unpaid_ppv_stays_excluded():
    """recentlyOffered is per-session: an UNSOLD free teaser resurfaces in a new
    session, but a still-UNPAID PPV (pendingOffers) stays excluded so we never
    re-send a locked item — we nudge the existing one instead."""
    from datetime import datetime, timedelta, timezone
    store.add_catalog_item("free1", "beach pic", ["beach"], heat=2, suggested_price_cents=0)
    store.add_catalog_item("ppv1", "spicy set", ["spicy"], heat=4, suggested_price_cents=5000)

    # session 1: send the free teaser AND the PPV
    menu = _menu("EXPLICIT")
    tools.send_media.invoke({"media_id": _id_for(menu, "free1"), "caption": "a"})
    tools.send_media.invoke({"media_id": _id_for(menu, "ppv1"), "caption": "b"})
    fan = store.get_fan("sim:f")
    assert {"free1", "ppv1"} <= set(fan["recentlyOfferedUuids"])
    assert any(o["mediaUuid"] == "ppv1" for o in fan["pendingOffers"])

    # same session → both excluded from the menu
    menu2 = _menu("EXPLICIT")
    assert _id_for(menu2, "free1") is None and _id_for(menu2, "ppv1") is None

    # force a NEW session (backdate activity + last-PPV so the gap + cooldown clear)
    f = store.get_fan("sim:f")
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    f["lastSeenAt"] = old
    f["lastPpvAt"] = old
    store.save_fan(f)
    store.compute_session("sim:f")

    menu3 = _menu("EXPLICIT")
    assert _id_for(menu3, "free1") is not None   # unsold free teaser comes back
    assert _id_for(menu3, "ppv1") is None         # still-unpaid PPV stays excluded


def test_send_failure_reports_and_does_not_record(monkeypatch):
    """A delivery error surfaces as 'send_failed' and records NO offer (so Jenny
    never claims a photo that didn't land)."""
    _seed()
    from fanvue_client import fanvue
    monkeypatch.setattr(fanvue, "_tokens", {"access_token": "x", "refresh_token": "y"})  # ready
    monkeypatch.setattr(fanvue, "send_message",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    menu = _menu("EXPLICIT", fan="real:fail")
    tools.update_context(dry_run=False)  # force a real send attempt (which will raise)
    assert tools.send_media.invoke({"media_id": _id_for(menu, "ppv_explicit"), "caption": "x"}) == "send_failed"
    assert "ppv_explicit" not in store.get_fan("real:fail").get("recentlyOfferedUuids", [])
