"""Content bundles: sold as a themed set, members also sell standalone, owned
members are excluded and the price adjusts. All deterministic — no LLM."""
import store
import tools


def _seed():
    store.add_catalog_item("gno_a", "lingerie tease set", ["lingerie"], heat=3, suggested_price_cents=800)
    store.add_catalog_item("gno_b", "cocktail dress set", ["dress"], heat=3, suggested_price_cents=700)
    store.add_bundle("bundle:gno", "Girls Night Out", "a wild night out with the girls",
                     ["girls", "night", "out"], heat=3, items=["gno_a", "gno_b"],
                     bundle_price_cents=1200)


def _ctx(mode, monetize=True, fan="sim:f"):
    tools.set_context(fan_id=fan, user_uuid=fan, dry_run=True)
    tools.update_context(mode=mode, monetize_ok=monetize)


def test_sends_full_bundle_when_none_owned():
    _seed(); _ctx("FLIRTY")
    res = tools.offer_bundle.invoke({"theme": "girls night out", "caption": "come out with us 😏"})
    assert res == "sent::Girls Night Out::$12.00::2"
    out = tools.get_outbox()[-1]
    assert out["type"] == "bundle"
    assert set(out["mediaUuids"]) == {"gno_a", "gno_b"}
    assert out["price_cents"] == 1200
    # both members recorded as offered
    offered = store.get_fan("sim:f")["recentlyOfferedUuids"]
    assert set(offered) == {"gno_a", "gno_b"}


def test_excludes_owned_member_and_adjusts_price():
    _seed(); _ctx("FLIRTY")
    store.add_purchase("sim:f", "inv1", gross_cents=800, source="message", media_uuids=["gno_a"])
    res = tools.offer_bundle.invoke({"theme": "girls night", "caption": "the rest of the set 😏"})
    # only gno_b left; price = 1200 * 700/1500 = 560
    assert res == "sent::Girls Night Out::$5.60::1"
    out = tools.get_outbox()[-1]
    assert out["mediaUuids"] == ["gno_b"]
    assert out["price_cents"] == 560


def test_nothing_when_all_members_owned():
    _seed(); _ctx("FLIRTY")
    store.add_purchase("sim:f", "inv1", gross_cents=1500, source="message",
                       media_uuids=["gno_a", "gno_b"])
    assert tools.offer_bundle.invoke({"theme": "girls", "caption": "x"}) == "nothing"


def test_not_now_when_mode_too_cool():
    _seed(); _ctx("WARM")
    assert tools.offer_bundle.invoke({"theme": "girls", "caption": "x"}) == "not_now"


def test_not_now_when_monetize_false():
    _seed(); _ctx("FLIRTY", monetize=False)
    assert tools.offer_bundle.invoke({"theme": "girls", "caption": "x"}) == "not_now"


def test_heat_gated_bundle_not_offered():
    # bundle hotter than current mode → not eligible
    store.add_catalog_item("x1", "explicit a", ["spicy"], heat=4, suggested_price_cents=900)
    store.add_catalog_item("x2", "explicit b", ["spicy"], heat=4, suggested_price_cents=900)
    store.add_bundle("bundle:hot", "Hot Set", "very spicy", ["spicy"], heat=4,
                     items=["x1", "x2"], bundle_price_cents=1500)
    _ctx("FLIRTY")
    assert tools.offer_bundle.invoke({"theme": "spicy", "caption": "x"}) == "nothing"


def test_recently_offered_members_not_reoffered(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "PPV_COOLDOWN_MINUTES", 0)   # ignore time cooldown
    monkeypatch.setattr(settings, "MAX_OFFERS_PER_SESSION", 5)  # ignore count cooldown
    _seed(); _ctx("FLIRTY")
    assert tools.offer_bundle.invoke({"theme": "girls", "caption": "a"}).startswith("sent::")
    # all members already offered → nothing new to show
    assert tools.offer_bundle.invoke({"theme": "girls", "caption": "b"}) == "nothing"
