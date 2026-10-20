"""Offline agent eval harness — Phase 1 (deterministic checks, REAL voice model).

Drives scripted fan messages through the real LangGraph agent and asserts
deterministic properties of the reply + tool actions:
  - character/leak: no meta-narration, no AI reveal, no prompt leak (EVERY case)
  - tone: <=1 emoji, no em-dash (every case unless tone_ok=False)
  - tool behavior: sent a photo when it should / stayed silent when it shouldn't
  - flow: the classifier lands the expected mode

NOT run in PR CI — it calls the paid model and is nondeterministic. Run on demand:

    make eval           # from repo root
    # or:  cd agent && OPENROUTER_API_KEY=... uv run python -m evals.run

Exit code: 0 = all pass, 1 = failures, 2 = skipped (no API key).
"""
from __future__ import annotations

import sys

import graph
import llm  # noqa: F401  (imported so the real voice model is wired; no fake set)
import modes
import sanitize
import store
from config import settings
from evals.cases import CASES

MEDIA_ACTIONS = {"teaser", "ppv", "bundle"}


def _seed(catalog):
    store.reset_memory()
    for c in catalog:
        store.add_catalog_item(c["uuid"], c["desc"], c.get("tags", []),
                               heat=c["heat"], suggested_price_cents=c.get("price", 0))


def _run_case(case):
    _seed(case.get("catalog", []))
    fan = case.get("fan", "sim:eval")
    forced = case.get("force_mode")
    original = modes.classify
    if forced:
        modes.classify = lambda msgs, cur, _m=forced: {"mode": _m, "escalate_ok": True, "reason": "forced"}
    try:
        out = graph.handle_message(fan, fan, case["say"], dry_run=True)
    finally:
        modes.classify = original
    return out.get("reply") or "", out.get("actions") or [], out.get("mode")


def _check(case, reply, actions, mode):
    exp = case.get("expect", {})
    fails = []

    # always-on character / leak guards
    if sanitize.has_meta_leak(reply):
        fails.append("meta-narration leak in reply")
    if sanitize.reveals_ai(reply):
        fails.append("reveals she's an AI")
    if sanitize.reveals_prompt(reply):
        fails.append("leaks prompt/system text")
    if exp.get("tone_ok", True):
        for issue in sanitize.tone_issues(reply):
            fails.append(f"tone: {issue}")

    # tool expectations
    fired = [a for a in actions if a.get("type") in MEDIA_ACTIONS]
    if exp.get("tool") == "send_media" and not fired:
        fails.append("expected a media send; none fired")
    if exp.get("tool") == "none" and fired:
        fails.append(f"expected NO media send; got {[a.get('type') for a in fired]}")
    if exp.get("action_type") and not any(a.get("type") == exp["action_type"] for a in fired):
        fails.append(f"expected action_type={exp['action_type']}; got {[a.get('type') for a in fired]}")

    # flow / mode
    if exp.get("mode") and mode != exp["mode"]:
        fails.append(f"expected mode={exp['mode']}; got {mode}")
    if exp.get("mode_in") and mode not in exp["mode_in"]:
        fails.append(f"expected mode in {exp['mode_in']}; got {mode}")

    return fails


def main():
    if not settings.OPENROUTER_API_KEY:
        print("SKIP: evals need the real model — set OPENROUTER_API_KEY.")
        return 2

    results = []
    for case in CASES:
        try:
            reply, actions, mode = _run_case(case)
            fails = _check(case, reply, actions, mode)
        except Exception as e:
            reply, fails = "", [f"ERROR {type(e).__name__}: {e}"]
        results.append((case, reply, fails))

    passed = sum(1 for _, _, f in results if not f)
    failed = len(results) - passed
    print(f"\n=== AGENT EVAL — {passed} passed, {failed} failed (of {len(results)}) ===\n")
    for case, reply, fails in results:
        mark = "PASS" if not fails else "FAIL"
        print(f"[{mark}] ({case['category']}) {case['name']}")
        if fails:
            print(f"       reply: {reply[:140]!r}")
            for f in fails:
                print(f"       - {f}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
