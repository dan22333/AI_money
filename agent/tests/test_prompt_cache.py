"""Prompt-caching guarantee: the static SYSTEM_PREFIX must be byte-identical on
every request (so the provider's prefix cache hits), and NO per-fan context may
leak into it. If someone later interpolates a fan detail into the prefix, these
tests fail."""
from conftest import ai

import graph
import llm
import modes
import store


class RecordingChat:
    """Voice model that records the messages it was invoked with, then replies."""
    last_messages = None

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        RecordingChat.last_messages = messages
        return ai(text="hey you 💕")


def _run(monkeypatch, fan_id, mode, incoming):
    monkeypatch.setattr(modes, "classify",
                        lambda msgs, cur: {"mode": mode, "escalate_ok": True,
                                           "monetize_ok": True, "reason": "t"})
    llm.set_voice_model(RecordingChat())
    graph.handle_message(fan_id, fan_id, incoming, dry_run=True)
    msgs = RecordingChat.last_messages
    llm.set_voice_model(None)
    return msgs


def test_prefix_is_stable_across_fans(monkeypatch):
    # fan A: whale, explicit, with a purchase + remembered fact
    store.add_purchase("sim:a", "INV1", gross_cents=5000, source="message")
    graph.memory.add_fact("sim:a", "his name is Mike")
    a = _run(monkeypatch, "sim:a", "EXPLICIT", "hey babe")
    # fan B: brand new, cold, nothing known
    b = _run(monkeypatch, "sim:b", "COLD", "hi")

    # 1) first system message is the static prefix, identical for both fans
    assert a[0].content == graph.SYSTEM_PREFIX
    assert b[0].content == graph.SYSTEM_PREFIX
    assert a[0].content == b[0].content

    # 2) the prefix carries persona + operating instructions, NO per-fan data
    assert "send_media" in graph.SYSTEM_PREFIX
    assert "Mike" not in graph.SYSTEM_PREFIX
    assert "spentCents" not in graph.SYSTEM_PREFIX

    # 3) per-fan context lives in the SECOND system message
    assert "FAN PROFILE" in a[1].content
    assert "Mike" in a[1].content          # remembered fact surfaced for A only
    assert "Mike" not in b[1].content
    assert a[1].content != b[1].content


def test_pending_offers_note_surfaces_locked_ppvs():
    """Jenny's per-fan context must flag LOCKED/unpaid PPVs so she doesn't assume
    the fan has seen them; empty string when there are none (no noise)."""
    assert graph._pending_offers_note([]) == ""
    note = graph._pending_offers_note([{"mediaUuid": "m1", "priceCents": 5000}])
    assert "AWAITING PAYMENT" in note and "$50" in note
    # the per-fan pending DATA rides in the dynamic context, never the cached prefix
    # (the generic "PAID CONTENT IS LOCKED" *instruction* may live in the prefix).
    assert "AWAITING PAYMENT" not in graph.SYSTEM_PREFIX
    ctx = graph._dynamic_context("p", "f", "s", "EXPLICIT", [{"mediaUuid": "m1", "priceCents": 5000}])
    assert "AWAITING PAYMENT" in ctx
