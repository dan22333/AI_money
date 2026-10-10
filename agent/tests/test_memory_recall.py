"""Recall/add contract against a FAKE mem0 client — offline.

The real pgvector/Vertex path can't run in CI, but these lock in OUR integration
with mem0's API shape: facts added route to the client, recall formats the
documented {"results":[{"memory":...}]} response, sim/real use SEPARATE
collections (staging isolation), and selftest reports honestly.
"""
import memory
import store
from config import settings


class FakeMem0:
    """Minimal stand-in for mem0.Memory: per-user list, search returns recents."""
    def __init__(self, collection):
        self.collection = collection
        self.data: dict[str, list[str]] = {}

    def add(self, messages, user_id):
        self.data.setdefault(user_id, []).extend(m["content"] for m in messages)

    def search(self, query, user_id, limit=6):
        return {"results": [{"memory": c} for c in self.data.get(user_id, [])[-limit:]]}


def _enable_fake(monkeypatch):
    """Turn memory on and make _client_for hand back a per-namespace FakeMem0."""
    monkeypatch.setattr(settings, "MEMORY_ENABLED", True)
    fakes = {}

    def fake_client_for(ns):
        return fakes.setdefault(ns, FakeMem0(memory._collection_for(ns)))

    monkeypatch.setattr(memory, "_client_for", fake_client_for)
    return fakes


def test_recall_surfaces_stored_fact(monkeypatch):
    _enable_fake(monkeypatch)
    store.use_namespace_for("fan-real")  # real namespace
    memory.add_fact("fan-real", "his name is Mike and he loves tennis")
    out = memory.recall("fan-real", "what's his name")
    assert "Mike" in out and out.startswith("- ")


def test_sim_and_real_are_isolated(monkeypatch):
    fakes = _enable_fake(monkeypatch)

    store.use_namespace_for("sim:probe")   # → sim collection
    memory.add_fact("sim:probe", "sim-only secret")
    store.use_namespace_for("fan-real")    # → real collection
    memory.add_fact("fan-real", "real fact")

    # different physical collections
    assert fakes["sim"].collection == "fan_memories_sim"
    assert fakes["real"].collection == "fan_memories"
    # a real fan never sees sim memories and vice-versa
    store.use_namespace_for("fan-real")
    assert "sim-only" not in memory.recall("sim:probe", "secret")


def test_selftest_ok_and_disabled(monkeypatch):
    # disabled → reports disabled, doesn't touch any client
    monkeypatch.setattr(settings, "MEMORY_ENABLED", False)
    assert memory.selftest()["status"] == "disabled"

    # enabled + working client → ok
    _enable_fake(monkeypatch)
    store.use_namespace_for("sim:__memcheck__")
    res = memory.selftest()
    assert res["status"] == "ok"
    assert res["collection"] == "fan_memories_sim"
