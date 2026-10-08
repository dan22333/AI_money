"""End-to-end run of the real LangGraph graph with a FAKE voice model — offline
and deterministic. Proves wiring: context → mode → tool call → send → persist,
and the no-re-sell guarantee holds across turns."""
from conftest import FakeChat, ai

import graph
import llm
import modes
import store


def _force_mode(monkeypatch, mode="EXPLICIT", monetize=True):
    monkeypatch.setattr(modes, "classify",
                        lambda msgs, cur: {"mode": mode, "escalate_ok": True,
                                           "monetize_ok": monetize, "reason": "test"})


def test_agent_sells_then_never_resells(monkeypatch):
    _force_mode(monkeypatch)
    store.add_catalog_item("ppv1", "spicy bedroom set", ["spicy", "bedroom"], heat=4,
                           suggested_price_cents=1500)

    # turn 1: model asks to sell, then speaks
    llm.set_voice_model(FakeChat([
        ai(tool="offer_content", args={"theme": "bedroom", "caption": "earned this 😏"}),
        ai(text="hope you love it babe 🙈"),
    ]))
    out = graph.handle_message("sim:mike", "sim:mike", "tell me what you'd do to me", dry_run=True)
    assert out["reply"] == "hope you love it babe 🙈"
    assert out["mode"] == "EXPLICIT"
    ppv = [a for a in out["actions"] if a["type"] == "ppv"]
    assert ppv and ppv[0]["mediaUuid"] == "ppv1" and ppv[0]["price_cents"] == 1500

    # simulate the fan buying it
    store.add_purchase("sim:mike", "FV-9", gross_cents=1500, source="message",
                       media_uuids=["ppv1"])

    # turn 2: model tries to sell the same theme again → tool declines ("nothing"),
    # model falls back to a plain reply. No second PPV for the owned item.
    llm.set_voice_model(FakeChat([
        ai(tool="offer_content", args={"theme": "bedroom", "caption": "want more? 😏"}),
        ai(text="mmm glad you liked that one"),
    ]))
    out2 = graph.handle_message("sim:mike", "sim:mike", "that was hot, more?", dry_run=True)
    ppv2 = [a for a in out2["actions"] if a["type"] == "ppv"]
    assert ppv2 == []  # never re-sold the purchased item
    assert out2["reply"] == "mmm glad you liked that one"

    llm.set_voice_model(None)  # reset


def test_plain_reply_no_tools(monkeypatch):
    _force_mode(monkeypatch, mode="WARM", monetize=False)
    llm.set_voice_model(FakeChat([ai(text="heyy how was your day? 💕")]))
    out = graph.handle_message("sim:jo", "sim:jo", "hi jenny", dry_run=True)
    assert out["reply"] == "heyy how was your day? 💕"
    assert out["actions"] == []
    # persisted transcript for this sim fan
    assert store.message_count("sim:jo") == 2  # fan + jenny
    llm.set_voice_model(None)


def test_sim_namespace_isolated(monkeypatch):
    _force_mode(monkeypatch, mode="WARM", monetize=False)
    llm.set_voice_model(FakeChat([ai(text="hi")]))
    graph.handle_message("sim:test", "sim:test", "yo", dry_run=True)
    assert store.get_fan("sim:test")["source"] == "sim"
    llm.set_voice_model(None)
