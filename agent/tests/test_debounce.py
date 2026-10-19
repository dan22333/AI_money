"""Per-fan debounce: lease, watermark, batch query, and the worker flow.

These exercise the Scenario A/B/C logic from docs/debounce without Cloud Tasks or
Fanvue — enqueue + Fanvue read-back are monkeypatched, and the agent generation
(graph.handle_batch) is faked so we assert on the ORCHESTRATION (what batch gets
answered, how the watermark moves, how the lock behaves)."""
from datetime import timedelta

from conftest import FakeChat, ai

import debounce
import graph
import llm
import modes
import router
import store
import tasks
from config import settings


def _write_fan_msg(fan, uuid, text, *, age_seconds=0):
    """Write a fan message aged `age_seconds` in the past (so the quiet check sees
    it as settled when age > SETTLE)."""
    real_now = store._now
    store._now = lambda: real_now() - timedelta(seconds=age_seconds)
    try:
        store.upsert_message(fan, uuid, "fan", text, via="webhook")
    finally:
        store._now = real_now


# ---------------- lease ----------------
def test_lease_acquire_release_and_steal_on_expiry():
    fan = "sim:lease"
    assert store.acquire_lease(fan, "ownerA", ttl_seconds=100) is True
    # a different worker can't grab a live lease
    assert store.acquire_lease(fan, "ownerB", ttl_seconds=100) is False
    # the holder releasing frees it
    store.release_lease(fan, "ownerA")
    assert store.acquire_lease(fan, "ownerB", ttl_seconds=100) is True
    # force expiry → a new worker may steal it (dead-worker self-heal)
    f = store.get_fan(fan)
    f["processingLease"]["expiresAt"] = (store._now() - timedelta(seconds=1)).isoformat()
    store.save_fan(f)
    assert store.acquire_lease(fan, "ownerC", ttl_seconds=100) is True


def test_release_only_clears_own_lease():
    fan = "sim:lease2"
    store.acquire_lease(fan, "ownerA", ttl_seconds=100)
    store.release_lease(fan, "someone-else")  # no-op
    assert store.get_fan(fan)["processingLease"]["owner"] == "ownerA"


# ---------------- watermark + batch ----------------
def test_unanswered_excludes_below_watermark():
    fan = "sim:wm"
    _write_fan_msg(fan, "a", "old buried", age_seconds=100)
    first_ts = store.unanswered_fan_messages(fan)[0]["ts"]
    store.finish_processing(fan, owner="x", through_ts=first_ts)  # "we replied up to here"
    assert store.unanswered_fan_messages(fan) == []  # the one msg is now answered
    _write_fan_msg(fan, "b", "new tail", age_seconds=1)
    pending = store.unanswered_fan_messages(fan)
    assert [m["text"] for m in pending] == ["new tail"]


def test_advance_watermark_is_monotonic():
    fan = "sim:mono"
    store.advance_watermark_to(fan, "2026-01-02T00:00:00+00:00")
    store.advance_watermark_to(fan, "2026-01-01T00:00:00+00:00")  # older → ignored
    assert store.get_fan(fan)["lastProcessedAt"] == "2026-01-02T00:00:00+00:00"


# ---------------- worker flow ----------------
def _fake_generation(monkeypatch):
    """Record what batch boundary handle_batch was asked to answer."""
    calls = []
    monkeypatch.setattr(graph, "handle_batch",
                        lambda fan, uuid, through_ts, batch_text: calls.append(
                            {"through": through_ts, "text": batch_text}) or
                        {"reply": "one reply", "mode": "WARM", "actions": []})
    monkeypatch.setattr(tasks, "enqueue_process_fan", lambda *a, **k: True)
    return calls


def test_scenario_a_two_messages_one_reply(monkeypatch):
    monkeypatch.setattr(settings, "DEBOUNCE_SETTLE_SECONDS", 8.0)
    calls = _fake_generation(monkeypatch)
    fan = "sim:A"
    _write_fan_msg(fan, "a", "hey", age_seconds=30)
    _write_fan_msg(fan, "b", "you up?", age_seconds=30)
    out = debounce.process_fan(fan, fan)
    assert out["status"] == "replied"
    assert len(calls) == 1                       # ONE reply for both
    assert "hey" in calls[0]["text"] and "you up?" in calls[0]["text"]
    # watermark advanced to the newest message; nothing left pending; lock freed
    assert store.unanswered_fan_messages(fan) == []
    assert store.get_fan(fan)["processingLease"] is None


def test_not_quiet_defers_and_reenqueues(monkeypatch):
    """Not-settled task must RE-ENQUEUE a follow-up (not just bail) — else a last
    message whose own task fired slightly early (Cloud Tasks jitter) is orphaned
    forever. Regression for the 'answer gap' bug."""
    monkeypatch.setattr(settings, "DEBOUNCE_SETTLE_SECONDS", 8.0)
    calls = _fake_generation(monkeypatch)
    reenq = []
    monkeypatch.setattr(tasks, "enqueue_process_fan", lambda f, u, d: reenq.append(d) or True)
    fan = "sim:Q"
    _write_fan_msg(fan, "a", "just now", age_seconds=0)  # fresh → not quiet yet
    out = debounce.process_fan(fan, fan)
    assert out["status"] == "not_quiet"
    assert calls == []                           # didn't generate
    assert store.get_fan(fan).get("processingLease") in (None, {})  # never locked
    assert len(reenq) == 1 and reenq[0] > 0      # scheduled a follow-up → no orphan


def test_busy_reenqueues(monkeypatch):
    monkeypatch.setattr(settings, "DEBOUNCE_SETTLE_SECONDS", 8.0)
    _fake_generation(monkeypatch)
    enq = []
    monkeypatch.setattr(tasks, "enqueue_process_fan", lambda f, u, d: enq.append(d) or True)
    fan = "sim:B"
    _write_fan_msg(fan, "a", "hi", age_seconds=30)
    store.acquire_lease(fan, "someone-live", ttl_seconds=100)  # held by another worker
    out = debounce.process_fan(fan, fan)
    assert out["status"] == "busy"
    assert enq == [settings.PROCESS_BUSY_RETRY_SECONDS]


def test_retry_readback_skips_resend(monkeypatch):
    monkeypatch.setattr(settings, "DEBOUNCE_SETTLE_SECONDS", 8.0)
    calls = _fake_generation(monkeypatch)
    monkeypatch.setattr(debounce, "_jenny_replied_since", lambda uuid, ts: True)
    fan = "sim:R"
    _write_fan_msg(fan, "a", "already answered", age_seconds=30)
    out = debounce.process_fan(fan, fan, is_retry=True)
    assert out["status"] == "already_sent"
    assert calls == []                           # did NOT regenerate/resend
    assert store.unanswered_fan_messages(fan) == []   # still marked answered
    assert store.get_fan(fan)["processingLease"] is None


def test_learn_runs_after_lease_released(monkeypatch):
    """mem0 extraction + summary (learn_from_turn) must run AFTER the lease is
    released, so they don't block the next batch for this fan."""
    monkeypatch.setattr(settings, "DEBOUNCE_SETTLE_SECONDS", 8.0)
    _fake_generation(monkeypatch)
    seen = {}
    monkeypatch.setattr(graph, "learn_from_turn",
                        lambda fan, inc, final: seen.update(
                            lease=store.get_fan(fan).get("processingLease"),
                            incoming=inc, final=final))
    fan = "sim:learn"
    _write_fan_msg(fan, "a", "remember I love tennis", age_seconds=30)
    out = debounce.process_fan(fan, fan)
    assert out["status"] == "replied"
    assert seen["lease"] is None            # lock already released when learn ran
    assert seen["incoming"] == "remember I love tennis"
    assert seen["final"] == "one reply"


def test_error_releases_lease_and_raises(monkeypatch):
    monkeypatch.setattr(settings, "DEBOUNCE_SETTLE_SECONDS", 8.0)
    monkeypatch.setattr(tasks, "enqueue_process_fan", lambda *a, **k: True)

    def boom(*a, **k):
        raise RuntimeError("llm down")

    monkeypatch.setattr(graph, "handle_batch", boom)
    fan = "sim:E"
    _write_fan_msg(fan, "a", "hi", age_seconds=30)
    try:
        debounce.process_fan(fan, fan)
        assert False, "should have raised"
    except RuntimeError:
        pass
    # watermark NOT advanced (so a retry regenerates), lease released
    assert store.unanswered_fan_messages(fan)            # still pending
    assert store.get_fan(fan)["processingLease"] is None


# ---------------- the three canonical scenarios (docs/debounce) ----------------
# Wall-clock is simulated by patching _seconds_since to a holder value, so "how
# long ago was the newest message" is whatever we say at each worker invocation.
def _sim_clock(monkeypatch, holder):
    monkeypatch.setattr(debounce, "_seconds_since", lambda ts: holder["quiet"])


def _capture_enqueues(monkeypatch):
    enq = []
    monkeypatch.setattr(tasks, "enqueue_process_fan",
                        lambda f, u, d: enq.append(d) or True)
    return enq


def test_scenario_A_two_messages_within_settle_one_reply(monkeypatch):
    """A + B arrive 1.5s apart (both inside the settle window). A's task fires first
    and finds the fan NOT quiet (B is newer-than-settle) → defers. B's task fires
    when quiet → ONE reply covering both."""
    monkeypatch.setattr(settings, "DEBOUNCE_SETTLE_SECONDS", 8.0)
    calls = _fake_generation(monkeypatch)
    holder = {"quiet": 0.5}          # newest message is still "fresh"
    _sim_clock(monkeypatch, holder)
    fan = "sim:scenA"
    _write_fan_msg(fan, "a", "hey", age_seconds=2)
    _write_fan_msg(fan, "b", "you up?", age_seconds=1)   # 1.5s-ish after A, newer

    out_a = debounce.process_fan(fan, fan)               # Task-A fires: not quiet yet
    assert out_a["status"] == "not_quiet"
    assert calls == []

    holder["quiet"] = 10                                 # now the burst has settled
    out_b = debounce.process_fan(fan, fan)               # Task-B fires: quiet → reply
    assert out_b["status"] == "replied"
    assert len(calls) == 1                               # exactly ONE reply...
    assert "hey" in calls[0]["text"] and "you up?" in calls[0]["text"]  # ...for both
    assert store.unanswered_fan_messages(fan) == []      # batch fully answered


def test_scenario_B_gap_non_overlapping_two_replies(monkeypatch):
    """A is fully answered (reply committed) BEFORE B arrives — a real gap. B gets
    its own reply, generated with A + reply-1 already in history (A is below the
    watermark, B is the only pending message)."""
    monkeypatch.setattr(settings, "DEBOUNCE_SETTLE_SECONDS", 8.0)
    calls = _fake_generation(monkeypatch)
    holder = {"quiet": 10}           # every worker here runs after the fan went quiet
    _sim_clock(monkeypatch, holder)
    fan = "sim:scenB"

    _write_fan_msg(fan, "a", "morning", age_seconds=30)
    a_ts = store.unanswered_fan_messages(fan)[0]["ts"]
    out1 = debounce.process_fan(fan, fan)                # answers A
    assert out1["status"] == "replied" and out1["through"] == a_ts
    assert store.get_fan(fan)["lastProcessedAt"] == a_ts  # A now below watermark

    _write_fan_msg(fan, "b", "you there?", age_seconds=1)  # arrives after the gap
    out2 = debounce.process_fan(fan, fan)                # answers B separately
    assert out2["status"] == "replied"
    assert len(calls) == 2                               # TWO distinct replies
    assert calls[0]["text"] == "morning"                 # first batch = just A
    assert calls[1]["text"] == "you there?"              # second batch = just B
    assert out2["through"] == store.get_fan(fan)["lastProcessedAt"]


def test_scenario_C_message_during_generation_handled_after_in_order(monkeypatch):
    """C lands while the A+B worker holds the lock. C's task fires → sees the lock
    held → re-enqueues itself (can't jump the queue). After A+B commits, C's task
    runs and answers C ALONE, with A+B (and their reply) already below the watermark."""
    monkeypatch.setattr(settings, "DEBOUNCE_SETTLE_SECONDS", 8.0)
    calls = _fake_generation(monkeypatch)
    enq = _capture_enqueues(monkeypatch)
    holder = {"quiet": 10}
    _sim_clock(monkeypatch, holder)
    fan = "sim:scenC"

    _write_fan_msg(fan, "a", "A", age_seconds=30)
    _write_fan_msg(fan, "b", "B", age_seconds=20)
    b_ts = max(m["ts"] for m in store.unanswered_fan_messages(fan))
    # the A+B worker is mid-generation: it holds the lease
    assert store.acquire_lease(fan, "ab-worker", ttl_seconds=100) is True
    _write_fan_msg(fan, "c", "C", age_seconds=10)        # C arrives DURING generation

    out_busy = debounce.process_fan(fan, fan)            # C's task fires → lock held
    assert out_busy["status"] == "busy"
    assert enq == [settings.PROCESS_BUSY_RETRY_SECONDS]  # it re-enqueued itself
    assert calls == []                                   # C did NOT jump the queue

    # the A+B worker finishes: sends, commits (advances watermark to B, releases)
    store.finish_processing(fan, "ab-worker", b_ts)

    out_c = debounce.process_fan(fan, fan)               # C's re-enqueued task runs
    assert out_c["status"] == "replied"
    assert len(calls) == 1                               # one reply, for C only
    assert calls[0]["text"] == "C"                       # A and B excluded (answered)
    c_ts = store.get_fan(fan)["lastProcessedAt"]
    assert out_c["through"] == c_ts and c_ts > b_ts      # strictly after A+B


# ---------------- real-graph batch generation ----------------
def test_handle_batch_answers_history_without_duplicating(monkeypatch):
    """handle_batch runs the REAL graph over the stored batch: the fan messages are
    already the tail of history, so it must answer them WITHOUT re-appending an
    incoming turn, and persist exactly one jenny reply (fanvue not ready → no send)."""
    monkeypatch.setattr(modes, "classify",
                        lambda msgs, cur: {"mode": "WARM", "monetize_ok": False,
                                           "escalate_ok": True, "reason": "t"})
    import fanvue_client
    sent = []
    monkeypatch.setattr(fanvue_client.fanvue, "send_message",
                        lambda uuid, text, **k: sent.append(text) or {"uuid": "j1"})
    fan = "sim:batch"
    _write_fan_msg(fan, "a", "hey", age_seconds=30)
    _write_fan_msg(fan, "b", "you around?", age_seconds=30)
    through = max(m["ts"] for m in store.unanswered_fan_messages(fan))
    llm.set_voice_model(FakeChat([ai(text="heyy yes im here 💕")]))
    out = graph.handle_batch(fan, fan, through, "hey\nyou around?")
    llm.set_voice_model(None)
    assert out["reply"] == "heyy yes im here 💕"
    # 2 fan + 1 jenny; the two fan messages weren't re-written (still 2)
    assert store.message_count(fan) == 3
    roles = [m["role"] for m in store.recent_messages(fan, limit=10)]
    assert roles.count("fan") == 2 and roles.count("jenny") == 1


def test_system_note_not_treated_as_unanswered():
    """A synthetic internal prompt (purchase reaction) is stored kind='system' and
    must NOT show up as an unanswered fan message for the debounce worker."""
    fan = "sim:sys"
    store.save_message(fan, "fan", "[fan just unlocked ... react warmly]", kind="system")
    store.save_message(fan, "fan", "real question?")   # a genuine fan message
    pending = store.unanswered_fan_messages(fan)
    assert [m["text"] for m in pending] == ["real question?"]


# ---------------- router wiring ----------------
def test_router_debounce_queues(monkeypatch):
    monkeypatch.setattr(tasks, "enabled", lambda: True)
    enq = {}
    monkeypatch.setattr(tasks, "enqueue_process_fan",
                        lambda f, u, d: enq.update(fan=f, delay=d) or True)
    monkeypatch.setattr(graph, "handle_message",
                        lambda **k: (_ for _ in ()).throw(AssertionError("should not run inline")))
    ev = {"messageUuid": "m1", "sender": {"uuid": "fan-1"},
          "message": {"text": "hi"}, "eventId": "e-dq"}
    out = router.handle_event(ev, dry_run=False)
    assert out["status"] == "queued"
    assert enq["fan"] == "fan-1"
    # the message was recorded for the worker to pick up
    assert store.unanswered_fan_messages("fan-1")[0]["text"] == "hi"


def test_router_sync_fallback_when_enqueue_fails(monkeypatch):
    monkeypatch.setattr(tasks, "enabled", lambda: True)
    monkeypatch.setattr(tasks, "enqueue_process_fan", lambda *a, **k: False)  # enqueue failed
    ran = {}
    monkeypatch.setattr(graph, "handle_message",
                        lambda **k: ran.update(k) or {"reply": "hi", "mode": "WARM", "actions": []})
    ev = {"messageUuid": "m2", "sender": {"uuid": "fan-2"},
          "message": {"text": "yo"}, "eventId": "e-fb"}
    out = router.handle_event(ev, dry_run=False)
    assert out["status"] == "replied"            # fell back to synchronous
    assert ran["text"] == "yo"
