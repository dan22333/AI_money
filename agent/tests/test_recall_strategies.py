"""Recall strategies: context query + LLM query-expansion (HyDE-style).

Semantic quality can only be judged against the real pgvector store (use the
/admin/recall_eval endpoint for that). These lock in the WIRING offline: the
expansion LLM is faked, _search is stubbed to return controlled facts per probe,
and we assert the context query is built, probes drive the search, and results
merge + de-dupe. Backwards-compat: plain recall() still works."""
import llm
import memory
from config import settings


def test_join_turns_builds_context_query():
    turns = [{"role": "fan", "text": "hey"}, {"role": "jenny", "text": "hi babe"}]
    q = memory._join_turns(turns, "you around?")
    assert q == "fan: hey\njenny: hi babe\nfan: you around?"


def test_expand_queries_parses_llm(monkeypatch):
    monkeypatch.setattr(llm, "classify_json",
                        lambda sys, user: {"queries": ["the fan's job", " pets ", ""]})
    qs = memory.expand_queries("fan: I was so tired after my night shift", max_queries=3)
    assert qs == ["the fan's job", "pets"]        # trimmed, empties dropped


def test_expand_queries_empty_on_failure(monkeypatch):
    monkeypatch.setattr(llm, "classify_json",
                        lambda s, u: (_ for _ in ()).throw(RuntimeError("llm down")))
    assert memory.expand_queries("something") == []   # degrades, never raises
    assert memory.expand_queries("") == []            # no call on empty input


def test_recall_contextual_context_strategy_uses_joined_query(monkeypatch):
    seen = {}
    monkeypatch.setattr(memory, "_search",
                        lambda fan, query, limit=6: seen.update(q=query) or ["fact: tennis"])
    turns = [{"role": "fan", "text": "played tennis today"}]
    out = memory.recall_contextual("f", "and you?", turns, strategy="context")
    assert "played tennis today" in seen["q"] and "and you?" in seen["q"]
    assert out == "- fact: tennis"


def test_recall_contextual_expanded_merges_and_dedups(monkeypatch):
    monkeypatch.setattr(settings, "RECALL_EXPANSION_PROBES", 3)
    monkeypatch.setattr(llm, "classify_json",
                        lambda s, u: {"queries": ["job", "pets"]})
    # each probe returns a different fact, with one overlap to prove de-dup
    per_probe = {"job": ["works night shifts", "likes coffee"],
                 "pets": ["has a labrador", "likes coffee"]}
    monkeypatch.setattr(memory, "_search",
                        lambda fan, query, limit=6: per_probe.get(query, []))
    out = memory.recall_contextual("f", "hey", [{"role": "fan", "text": "hey"}],
                                   strategy="expanded")
    facts = [line[2:] for line in out.splitlines()]
    assert facts == ["works night shifts", "likes coffee", "has a labrador"]  # merged, deduped


def test_recall_contextual_expanded_falls_back_when_no_probes(monkeypatch):
    monkeypatch.setattr(llm, "classify_json", lambda s, u: {"queries": []})  # nothing to expand
    seen = {}
    monkeypatch.setattr(memory, "_search",
                        lambda fan, query, limit=6: seen.update(q=query) or [])
    memory.recall_contextual("f", "hey", [{"role": "fan", "text": "hey"}], strategy="expanded")
    assert "hey" in seen["q"]                       # fell back to the context/incoming query


def test_recall_eval_returns_all_three(monkeypatch):
    monkeypatch.setattr(llm, "classify_json", lambda s, u: {"queries": ["job"]})
    monkeypatch.setattr(memory, "_search", lambda fan, query, limit=6: ["a fact"])
    res = memory.recall_eval("f", "hey", [{"role": "fan", "text": "hey"}])
    assert set(res) == {"latest", "context", "expanded"}
    assert res["expanded"]["probes"] == ["job"]
    assert res["latest"]["facts"] == ["a fact"]


def test_plain_recall_still_works(monkeypatch):
    """Backwards compat: the simple recall() API is unchanged."""
    monkeypatch.setattr(memory, "_search", lambda fan, query, limit=6: ["his name is Mike"])
    assert memory.recall("f", "name") == "- his name is Mike"
    monkeypatch.setattr(memory, "_search", lambda fan, query, limit=6: [])
    assert memory.recall("f", "name") == "(nothing remembered yet)"
