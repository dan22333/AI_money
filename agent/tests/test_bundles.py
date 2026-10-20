"""Content bundles via the menu: a themed set appears as ONE menu entry (owned
members excluded, price adjusted), and send_media sends the whole unowned set under
one price. All deterministic — no LLM."""
import store
import tools


def _menu(mode, fan="sim:f"):
    tools.set_context(fan_id=fan, user_uuid=fan, dry_run=True)
    menu = store.sendable_menu(fan, mode)
    tools.update_context(mode=mode, menu=menu)
    return menu


def _bundle_id(menu, title):
    return next((e["id"] for e in menu.values()
                 if e["kind"] == "bundle" and e["desc"] == title), None)


def _seed():
    store.add_catalog_item("gno_a", "lingerie tease set", ["lingerie"], heat=3, suggested_price_cents=800)
    store.add_catalog_item("gno_b", "cocktail dress set", ["dress"], heat=3, suggested_price_cents=700)
    store.add_bundle("bundle:gno", "Girls Night Out", "a wild night out with the girls",
                     ["girls", "night", "out"], heat=3, items=["gno_a", "gno_b"],
                     bundle_price_cents=1200)


def test_sends_full_bundle_when_none_owned():
    _seed()
    menu = _menu("FLIRTY")
    bid = _bundle_id(menu, "Girls Night Out")
    assert bid
    res = tools.send_media.invoke({"media_id": bid, "caption": "come out with us 😏"})
    assert res == "sent::Girls Night Out::$12.00::2"
    out = tools.get_outbox()[-1]
    assert out["type"] == "bundle" and set(out["mediaUuids"]) == {"gno_a", "gno_b"}
    assert out["price_cents"] == 1200
    assert set(store.get_fan("sim:f")["recentlyOfferedUuids"]) == {"gno_a", "gno_b"}


def test_excludes_owned_member_and_adjusts_price():
    _seed()
    store.add_purchase("sim:f", "inv1", gross_cents=800, source="message", media_uuids=["gno_a"])
    menu = _menu("FLIRTY")
    bid = _bundle_id(menu, "Girls Night Out")
    res = tools.send_media.invoke({"media_id": bid, "caption": "the rest of the set 😏"})
    # only gno_b left; price = 1200 * 700/1500 = 560; single remaining item → no count suffix
    assert res == "sent::Girls Night Out::$5.60"
    out = tools.get_outbox()[-1]
    assert out["mediaUuids"] == ["gno_b"] and out["price_cents"] == 560


def test_owned_bundle_absent_from_menu():
    _seed()
    store.add_purchase("sim:f", "inv1", gross_cents=1500, source="message",
                       media_uuids=["gno_a", "gno_b"])
    assert _bundle_id(_menu("FLIRTY"), "Girls Night Out") is None


def test_bundle_absent_when_mode_too_cool():
    _seed()
    assert _bundle_id(_menu("WARM"), "Girls Night Out") is None


def test_heat_gated_bundle_absent_from_menu():
    store.add_catalog_item("x1", "explicit a", ["spicy"], heat=4, suggested_price_cents=900)
    store.add_catalog_item("x2", "explicit b", ["spicy"], heat=4, suggested_price_cents=900)
    store.add_bundle("bundle:hot", "Hot Set", "very spicy", ["spicy"], heat=4,
                     items=["x1", "x2"], bundle_price_cents=1500)
    assert _bundle_id(_menu("FLIRTY"), "Hot Set") is None   # hotter than mode


def test_recently_offered_bundle_drops_off_menu(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "PPV_COOLDOWN_MINUTES", 0)    # ignore time cooldown
    monkeypatch.setattr(settings, "MAX_OFFERS_PER_SESSION", 5)  # ignore count cooldown
    _seed()
    menu = _menu("FLIRTY")
    bid = _bundle_id(menu, "Girls Night Out")
    assert tools.send_media.invoke({"media_id": bid, "caption": "a"}).startswith("sent::")
    # both members now recently-offered → bundle no longer appears on a fresh menu
    assert _bundle_id(_menu("FLIRTY"), "Girls Night Out") is None
