"""Memory fallback + rolling-summary behavior — fully offline.

The live mem0/pgvector/Vertex path only turns on with MEMORY_ENABLED=true on GCP;
CI exercises the graceful fallback and the summary-refresh cadence.
"""
import memory
import summary
from config import settings


def test_memory_falls_back_when_disabled(monkeypatch):
    # MEMORY_ENABLED defaults false → mem0 never initializes, fallback is used.
    monkeypatch.setattr(settings, "MEMORY_ENABLED", False)
    memory._INIT_DONE = False
    memory.add_fact("sim:fan", "his name is Mike")
    memory.add_turn("sim:fan", "i work night shifts", "aww that's rough babe")
    out = memory.recall("sim:fan", "what do we know")
    assert "Mike" in out
    assert not memory._USING_MEM0  # stayed on fallback, never touched mem0


def test_recall_empty_message():
    assert memory.recall("sim:nobody", "anything") == "(nothing remembered yet)"


def test_summary_refreshes_on_cadence(monkeypatch):
    monkeypatch.setattr(summary.llm, "complete", lambda s, u, **k: "Mike, night-shift nurse, warm rapport.")
    fan = {"rollingSummary": "", "summarizedAtCount": 0}
    history = [{"role": "fan", "text": "hi"}, {"role": "jenny", "text": "hey"}]

    # below threshold → no refresh
    assert summary.maybe_update(fan, history, message_count=settings.SUMMARIZE_AFTER - 1) is False
    assert fan["rollingSummary"] == ""

    # at/over threshold → refresh + watermark advances
    assert summary.maybe_update(fan, history, message_count=settings.SUMMARIZE_AFTER) is True
    assert "Mike" in fan["rollingSummary"]
    assert fan["summarizedAtCount"] == settings.SUMMARIZE_AFTER

    # right after a refresh → not due again yet
    assert summary.maybe_update(fan, history, message_count=settings.SUMMARIZE_AFTER + 1) is False


def test_summary_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("model down")
    monkeypatch.setattr(summary.llm, "complete", boom)
    fan = {"rollingSummary": "old", "summarizedAtCount": 0}
    assert summary.maybe_update(fan, [{"role": "fan", "text": "x"}], message_count=999) is False
    assert fan["rollingSummary"] == "old"  # unchanged on failure
