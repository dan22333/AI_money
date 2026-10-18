"""Memory-scaling layer: Cloud Tasks enqueue, extraction fan-out, HNSW/pool config,
and the worker endpoints. Offline — the Cloud Tasks client and mem0 are never hit;
we assert the WIRING and the inline↔fan-out branching."""
from fastapi.testclient import TestClient

import graph
import main
import memory
import reconcile
import tasks
from config import settings

client = TestClient(main.app)


# ---------------- enqueue gating ----------------
def test_enqueue_gating_off_by_default(monkeypatch):
    # nothing configured → both gates closed, enqueue helpers return False (no client touched)
    monkeypatch.setattr(settings, "DEBOUNCE_ENABLED", False)
    monkeypatch.setattr(settings, "MEMORY_FANOUT_ENABLED", False)
    assert tasks.enabled() is False
    assert tasks.fanout_enabled() is False
    assert tasks.enqueue_process_fan("f", "f", 3) is False
    assert tasks.enqueue_extract_memory("f", "hi", "yo") is False
    assert tasks.enqueue_backfill_memory("f") is False


def test_fanout_needs_queue_and_url(monkeypatch):
    monkeypatch.setattr(settings, "MEMORY_FANOUT_ENABLED", True)
    monkeypatch.setattr(settings, "USE_FIRESTORE", True)
    monkeypatch.setattr(settings, "EXTRACT_TASKS_QUEUE", "jenny-extract")
    monkeypatch.setattr(settings, "SERVICE_BASE_URL", "")      # URL missing → still off
    assert tasks.fanout_enabled() is False
    monkeypatch.setattr(settings, "SERVICE_BASE_URL", "https://svc.run.app")
    assert tasks.fanout_enabled() is True


def test_enqueue_builds_request(monkeypatch):
    """_enqueue posts to our service URL + path with the sim-secret header and a
    JSON body. We fake the Cloud Tasks client to capture the request."""
    monkeypatch.setattr(settings, "SERVICE_BASE_URL", "https://svc.run.app")
    monkeypatch.setattr(settings, "SIM_SECRET", "shh")
    monkeypatch.setattr(settings, "TASKS_INVOKER_SA", "")
    captured = {}

    class FakeClient:
        def queue_path(self, proj, loc, queue):
            return f"{proj}/{loc}/{queue}"

        def create_task(self, request):
            captured.update(request)

    import types
    fake_tasks_v2 = types.SimpleNamespace(
        CloudTasksClient=lambda: FakeClient(),
        HttpMethod=types.SimpleNamespace(POST="POST"))
    import google.cloud
    monkeypatch.setattr(google.cloud, "tasks_v2", fake_tasks_v2, raising=False)
    monkeypatch.setitem(__import__("sys").modules, "google.cloud.tasks_v2", fake_tasks_v2)

    ok = tasks._enqueue("jenny-extract", "/admin/extract_memory", {"fan_id": "f"}, 0)
    assert ok is True
    hr = captured["task"]["http_request"]
    assert hr["url"] == "https://svc.run.app/admin/extract_memory"
    assert hr["headers"]["x-sim-secret"] == "shh"
    assert b'"fan_id"' in hr["body"]


# ---------------- maybe_learn: inline vs fan-out ----------------
def test_maybe_learn_inline_when_fanout_off(monkeypatch):
    monkeypatch.setattr(tasks, "enqueue_extract_memory", lambda *a, **k: False)  # not wired up
    called = {}
    monkeypatch.setattr(graph, "learn_from_turn",
                        lambda fan, inc, fin: called.update(fan=fan, inc=inc, fin=fin))
    graph.maybe_learn("fan-x", "hi", "hey babe")
    assert called == {"fan": "fan-x", "inc": "hi", "fin": "hey babe"}   # ran inline


def test_maybe_learn_enqueues_when_fanout_on(monkeypatch):
    enq = {}
    monkeypatch.setattr(tasks, "enqueue_extract_memory",
                        lambda fan, inc, fin: enq.update(fan=fan, inc=inc, fin=fin) or True)
    monkeypatch.setattr(graph, "learn_from_turn",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not run inline")))
    graph.maybe_learn("fan-y", "hi", "yo")
    assert enq == {"fan": "fan-y", "inc": "hi", "fin": "yo"}            # queued, not inline


# ---------------- mem0 config carries the scaling knobs ----------------
def test_mem0_config_passes_hnsw_and_pool(monkeypatch):
    monkeypatch.setattr(settings, "MEMORY_USE_HNSW", True)
    monkeypatch.setattr(settings, "MEMORY_PG_MINCONN", 2)
    monkeypatch.setattr(settings, "MEMORY_PG_MAXCONN", 12)
    cfg = memory._mem0_config("fan_memories")["vector_store"]["config"]
    assert cfg["hnsw"] is True and cfg["minconn"] == 2 and cfg["maxconn"] == 12


# ---------------- worker endpoints ----------------
def test_extract_memory_endpoint(monkeypatch):
    monkeypatch.setattr(settings, "SIM_SECRET", "shh")
    seen = {}
    monkeypatch.setattr(graph, "learn_from_turn",
                        lambda fan, inc, fin: seen.update(fan=fan, inc=inc, fin=fin))
    # auth required
    assert client.post("/admin/extract_memory", json={"fan_id": "f"}).status_code == 401
    r = client.post("/admin/extract_memory",
                    json={"fan_id": "sim:f", "incoming": "hi", "final": "hey"},
                    headers={"x-sim-secret": "shh"})
    assert r.status_code == 200 and r.json()["status"] == "learned"
    assert seen == {"fan": "sim:f", "inc": "hi", "fin": "hey"}


def test_backfill_memory_endpoint(monkeypatch):
    monkeypatch.setattr(settings, "SIM_SECRET", "shh")
    monkeypatch.setattr(reconcile, "_backfill_memory", lambda fan: 7)
    assert client.post("/admin/backfill_memory", json={"fan_id": "f"}).status_code == 401
    r = client.post("/admin/backfill_memory", json={"fan_id": "sim:f"},
                    headers={"x-sim-secret": "shh"})
    assert r.status_code == 200 and r.json()["extracted"] == 7


# ---------------- reconcile fans out the backfill when enabled ----------------
def test_reconcile_enqueues_backfill_when_fanout_on(monkeypatch):
    from test_reconcile import FakeFanvue
    fake = FakeFanvue(me={"uuid": "creator-0"},
                      chats=[{"user": {"uuid": "fan-a"}}, {"user": {"uuid": "fan-b"}}],
                      chat_messages={"fan-a": [{"uuid": "m1", "text": "hi",
                                                "sender": {"uuid": "fan-a"}, "sentAt": "2026-10-05T10:00:00Z"}],
                                     "fan-b": [{"uuid": "m2", "text": "yo",
                                                "sender": {"uuid": "fan-b"}, "sentAt": "2026-10-05T10:00:00Z"}]})
    monkeypatch.setattr(reconcile, "fanvue", fake)
    queued = []
    monkeypatch.setattr(reconcile.tasks, "enqueue_backfill_memory",
                        lambda fan: queued.append(fan) or True)
    monkeypatch.setattr(reconcile, "_backfill_memory",
                        lambda fan: (_ for _ in ()).throw(AssertionError("should enqueue, not inline")))
    out = reconcile.run()
    assert out["memoriesQueued"] == 2 and sorted(queued) == ["fan-a", "fan-b"]
