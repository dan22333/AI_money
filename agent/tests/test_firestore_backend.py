"""Runs the core store behaviours against the REAL Firestore backend via the
local emulator — the same code path used in production.

Skipped unless FIRESTORE_EMULATOR_HOST is set (so plain `pytest` stays offline).
In CI the `firestore-it` job starts the emulator, sets USE_FIRESTORE=true, and
runs: `pytest -m firestore`.
"""
import os

import pytest

import store

pytestmark = [
    pytest.mark.firestore,
    pytest.mark.skipif(not os.environ.get("FIRESTORE_EMULATOR_HOST"),
                       reason="requires Firestore emulator (set FIRESTORE_EMULATOR_HOST)"),
    pytest.mark.skipif(not getattr(__import__("config").settings, "USE_FIRESTORE", False),
                       reason="requires USE_FIRESTORE=true"),
]


def test_backend_is_firestore():
    assert type(store._backend()).__name__ == "FirestoreBackend"


def test_fan_roundtrip():
    f = store.get_fan("sim:fs")
    f["totalSpendCents"] = 500
    store.save_fan(f)
    assert store.get_fan("sim:fs")["totalSpendCents"] == 500


def test_messages_ordered_and_counted():
    for t in ["a", "b", "c"]:
        store.save_message("sim:fs", "fan", t, mode="WARM", session_id="s1")
    msgs = store.recent_messages("sim:fs", limit=2)
    assert [m["text"] for m in msgs] == ["b", "c"]  # chronological, newest two
    assert store.message_count("sim:fs") == 3


def test_purchase_idempotent_and_eligibility():
    store.add_catalog_item("x1", "spicy set", ["spicy"], heat=4, suggested_price_cents=1500)
    store.add_purchase("sim:fs", "inv1", gross_cents=1500, source="msg", media_uuids=["x1"])
    store.add_purchase("sim:fs", "inv1", gross_cents=1500, source="msg", media_uuids=["x1"])
    assert store.get_fan("sim:fs")["purchaseCount"] == 1
    ids = {i["mediaUuid"] for i in store.eligible_media("sim:fs", "EXPLICIT")}
    assert "x1" not in ids  # owned → filtered


def test_bundle_offer_on_firestore():
    store.add_catalog_item("b1", "lingerie", ["lingerie"], heat=3, suggested_price_cents=800)
    store.add_catalog_item("b2", "dress", ["dress"], heat=3, suggested_price_cents=700)
    store.add_bundle("bundle:x", "Night Out", "girls night", ["girls", "night"], heat=3,
                     items=["b1", "b2"], bundle_price_cents=1200)
    offer = store.best_bundle_offer("sim:fs", "FLIRTY", "girls night")
    assert offer and set(offer["items"]) == {"b1", "b2"} and offer["price_cents"] == 1200
