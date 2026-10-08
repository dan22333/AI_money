import store
import tools


def _ctx(mode, monetize=True, fan="sim:f"):
    tools.set_context(fan_id=fan, user_uuid=fan, dry_run=True)
    tools.update_context(mode=mode, monetize_ok=monetize)


def _seed():
    store.add_catalog_item("ppv_flirty", "lingerie tease set", ["lingerie"], heat=3,
                           suggested_price_cents=800)
    store.add_catalog_item("ppv_explicit", "spicy bedroom set", ["spicy", "bedroom"], heat=4,
                           suggested_price_cents=1500)


def test_not_now_when_mode_too_cool():
    _seed(); _ctx("WARM")
    assert tools.offer_content.invoke({"theme": "spicy", "caption": "hey"}) == "not_now"


def test_not_now_when_monetize_flag_false():
    _seed(); _ctx("EXPLICIT", monetize=False)
    assert tools.offer_content.invoke({"theme": "spicy", "caption": "hey"}) == "not_now"


def test_sells_eligible_item_and_records():
    _seed(); _ctx("EXPLICIT")
    res = tools.offer_content.invoke({"theme": "spicy bedroom", "caption": "just for you 😏"})
    assert res.startswith("sent::")
    out = tools.get_outbox()
    assert out and out[-1]["type"] == "ppv"
    assert out[-1]["mediaUuid"] == "ppv_explicit"
    assert out[-1]["price_cents"] == 1500
    # recorded as offered → cannot be offered again
    assert "ppv_explicit" in store.get_fan("sim:f")["recentlyOfferedUuids"]


def test_never_resells_purchased_item():
    # single paid item; once purchased there is nothing left to sell
    store.add_catalog_item("ppv_only", "spicy bedroom set", ["spicy", "bedroom"], heat=4,
                           suggested_price_cents=1500)
    _ctx("EXPLICIT")
    store.add_purchase("sim:f", "inv1", gross_cents=1500, source="message",
                       media_uuids=["ppv_only"])
    res = tools.offer_content.invoke({"theme": "bedroom", "caption": "x"})
    assert res == "nothing"


def test_cooldown_blocks_second_offer(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "MAX_OFFERS_PER_SESSION", 1)
    _seed(); _ctx("EXPLICIT")
    first = tools.offer_content.invoke({"theme": "spicy", "caption": "a"})
    assert first.startswith("sent::")
    second = tools.offer_content.invoke({"theme": "lingerie", "caption": "b"})
    assert second == "not_now"  # cooldown cap reached


def test_teaser_sends_free_item_only():
    store.add_catalog_item("free1", "gym selfie", ["gym"], heat=2, suggested_price_cents=0)
    store.add_catalog_item("paid1", "paid set", ["gym"], heat=2, suggested_price_cents=900)
    _ctx("WARM")
    res = tools.send_teaser.invoke({"theme": "gym", "caption": "hi"})
    assert res == "sent::gym selfie"
    assert tools.get_outbox()[-1]["mediaUuid"] == "free1"
