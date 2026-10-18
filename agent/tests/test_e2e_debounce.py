"""End-to-end (offline): the WHOLE flag-on debounce path, and expanded recall
through the real graph node.

Unlike test_debounce.py (which fakes graph.handle_batch to assert orchestration)
and test_recall_strategies.py (which unit-tests the recall functions), these drive
the real router → store → graph → worker wiring with only the external edges faked
(LLM via FakeChat, Fanvue send + Cloud Tasks enqueue stubbed). This is the "does
the whole thing actually hang together" layer."""
from conftest import FakeChat, ai

import debounce
import fanvue_client
import graph
import llm
import memory
import modes
import router
import store
import tasks
from config import settings


def _force_mode(monkeypatch, mode="WARM"):
    monkeypatch.setattr(modes, "classify",
                        lambda msgs, cur: {"mode": mode, "monetize_ok": False,
                                           "escalate_ok": True, "reason": "t"})


class RecordingChat(FakeChat):
    """FakeChat that also captures the messages it was asked to answer, so a test
    can assert what context (recalled facts) reached the model."""
    def __init__(self, script):
        super().__init__(script)
        self.seen_prompts = []

    def invoke(self, messages):
        self.seen_prompts.append(messages)
        return super().invoke(messages)


def test_e2e_debounce_webhook_to_single_reply(monkeypatch):
    """Two rapid webhooks (debounce ON) are recorded + queued (not answered inline);
    the worker then produces ONE real reply over the batch and advances the watermark."""
    _force_mode(monkeypatch)
    monkeypatch.setattr(tasks, "enabled", lambda: True)
    monkeypatch.setattr(tasks, "enqueue_process_fan", lambda f, u, d: True)   # don't really enqueue
    sent = []
    monkeypatch.setattr(fanvue_client.fanvue, "send_message",
                        lambda uuid, text, **k: sent.append(text) or {"uuid": "j-1"})
    # force the client "ready" so the send branch runs in CI too (CI has no real
    # token → ready would be False → the send is skipped and nothing lands in `sent`)
    monkeypatch.setattr(fanvue_client.fanvue, "_tokens", {"access_token": "x"})

    for i, (uuid, text) in enumerate([("m1", "hey"), ("m2", "you around?")]):
        ev = {"messageUuid": uuid, "sender": {"uuid": "fan-e2e"},
              "message": {"text": text}, "eventId": f"ev{i}"}
        assert router.handle_event(ev, dry_run=False)["status"] == "queued"
    assert sent == []                                            # nothing answered inline
    assert len(store.unanswered_fan_messages("fan-e2e")) == 2     # both recorded, pending

    # the Cloud Tasks worker fires once the fan is quiet
    monkeypatch.setattr(debounce, "_seconds_since", lambda ts: 10.0)
    llm.set_voice_model(FakeChat([ai(text="heyy im here 💕")]))
    res = debounce.process_fan("fan-e2e", "fan-e2e")
    llm.set_voice_model(None)

    assert res["status"] == "replied" and res["batchSize"] == 2
    assert sent == ["heyy im here 💕"]                            # exactly ONE send for the burst
    assert store.unanswered_fan_messages("fan-e2e") == []        # watermark advanced
    assert store.get_fan("fan-e2e")["processingLease"] is None   # lock released
    roles = [m["role"] for m in store.recent_messages("fan-e2e", limit=10)]
    assert roles.count("fan") == 2 and roles.count("jenny") == 1


def test_e2e_debounce_disabled_replies_inline(monkeypatch):
    """With debounce OFF (the shipped default), the webhook answers synchronously —
    proving the flag cleanly selects the old path."""
    _force_mode(monkeypatch)
    monkeypatch.setattr(tasks, "enabled", lambda: False)         # debounce off
    monkeypatch.setattr(fanvue_client.fanvue,
                        "send_message", lambda *a, **k: {"uuid": "j"})
    llm.set_voice_model(FakeChat([ai(text="hey you 💕")]))
    ev = {"messageUuid": "m1", "sender": {"uuid": "fan-sync"},
          "message": {"text": "hi"}, "eventId": "evs"}
    out = router.handle_event(ev, dry_run=False)
    llm.set_voice_model(None)
    assert out["status"] == "replied" and out["reply"] == "hey you 💕"


def test_e2e_expanded_recall_feeds_facts_into_prompt(monkeypatch):
    """RECALL_STRATEGY=expanded through the REAL load_context node: the LLM probes
    drive the search and the retrieved fact actually lands in the model's prompt."""
    monkeypatch.setattr(settings, "RECALL_STRATEGY", "expanded")
    _force_mode(monkeypatch)
    monkeypatch.setattr(llm, "classify_json", lambda s, u: {"queries": ["the fan's job"]})
    monkeypatch.setattr(memory, "_search",
                        lambda fan, q, limit=6: ["works night shifts"] if "job" in q else [])
    monkeypatch.setattr(fanvue_client.fanvue, "send_message", lambda *a, **k: {"uuid": "j"})

    chat = RecordingChat([ai(text="how was the night shift babe?")])
    llm.set_voice_model(chat)
    out = graph.handle_message("sim:recall-e2e", "sim:recall-e2e", "hey", dry_run=True)
    llm.set_voice_model(None)

    assert out["reply"] == "how was the night shift babe?"
    # the recalled fact reached the model via the dynamic-context system message
    prompt_text = " ".join(getattr(m, "content", "") for m in chat.seen_prompts[0])
    assert "works night shifts" in prompt_text
