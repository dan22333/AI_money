"""Agent eval harness — single/multi-turn, two targets, deterministic checks.

Each case replays its turns (eval plays the FAN) against ONE fan; Jenny answers each;
checks run per turn. Every reply is also screened for leaks/AI-reveal/prompt-leak,
phantom sends, and tone — using the SAME agent/sanitize.py the live path uses.

Targets:
  --target inprocess  (default)  imports the graph, in-memory store, memory OFF.
                                  Fast local `make eval`. Runs ALL cases (seeds the
                                  catalog, can pin the mode).
  --target blackbox --url URL     POSTs each turn to a deployed /simulate (sim fan →
                                  staging DB, memory ON). Used for recall + the deploy
                                  gate. SKIPS cases that need a seeded catalog / forced
                                  mode (can't control a remote).
  --gate                          Only run gate=True cases; demote tone to a warning
                                  (never block a deploy on a flaky tone nit). Critical
                                  checks (leak/AI/prompt/tool/mode) still fail the gate.

Exit: 0 all critical checks pass, 1 a critical check failed, 2 skipped (no model/url).
"""
from __future__ import annotations

import argparse
import sys
import uuid

import sanitize
from config import settings
from evals.cases import CASES

MEDIA = {"teaser", "ppv", "bundle"}
PAID = {"ppv", "bundle"}


# --------------------------------------------------------------------------- drivers
def _turns(case):
    if "turns" in case:
        return case["turns"]
    return [{"say": case["say"], "expect": case.get("expect", {})}]


def _run_inprocess(case):
    """Drive the case against the in-process graph with the real voice model."""
    import graph
    import modes
    import store

    store.reset_memory()
    for c in case.get("catalog", []):
        store.add_catalog_item(c["uuid"], c["desc"], c.get("tags", []),
                               heat=c["heat"], suggested_price_cents=c.get("price", 0))
    fan = "sim:eval-" + uuid.uuid4().hex[:8]

    orig_classify = modes.classify
    orig_max = settings.MAX_OFFERS_PER_SESSION
    if "max_offers" in case:
        settings.MAX_OFFERS_PER_SESSION = case["max_offers"]
    forced = case.get("force_mode")
    if forced:
        modes.classify = lambda m, c, _f=forced: {"mode": _f, "escalate_ok": True, "reason": "forced"}
    try:
        results = []
        for turn in _turns(case):
            out = graph.handle_message(fan, fan, turn["say"], dry_run=True)
            results.append((turn, out.get("reply") or "", out.get("actions") or [], out.get("mode")))
            for uuid_ in turn.get("buy", []):  # simulate a purchase between turns
                store.add_purchase(fan, f"evbuy-{uuid_}", gross_cents=5000, source="eval",
                                   media_uuids=[uuid_])
        return results
    finally:
        modes.classify = orig_classify
        settings.MAX_OFFERS_PER_SESSION = orig_max


def _run_blackbox(case, url):
    """Drive the case against a deployed /simulate (sim fan → staging)."""
    import httpx

    fan = "sim:eval-" + uuid.uuid4().hex[:8]
    headers = {"x-sim-secret": settings.SIM_SECRET} if settings.SIM_SECRET else {}
    results = []
    for turn in _turns(case):
        r = httpx.post(f"{url.rstrip('/')}/simulate", headers=headers,
                       json={"fan_id": fan, "text": turn["say"]}, timeout=60)
        r.raise_for_status()
        body = r.json()
        results.append((turn, body.get("reply") or "", body.get("actions") or [], body.get("mode")))
    return results


# --------------------------------------------------------------------------- checks
def _check_turn(expect, reply, actions, mode, *, strict):
    """Return (critical_fails, warns) for one turn's reply."""
    crit, warn = [], []

    # always-on character / leak guards (critical)
    if sanitize.has_meta_leak(reply):
        crit.append("meta-narration leak")
    if sanitize.reveals_ai(reply):
        crit.append("reveals she's an AI")
    if sanitize.reveals_prompt(reply):
        crit.append("leaks prompt text")

    fired = [a for a in actions if a.get("type") in MEDIA]
    # phantom send = implies a send but no media tool fired (MEASUREMENT → warn only)
    if sanitize.claims_send(reply) and not fired:
        warn.append("phantom send (implied a photo, no tool fired)")

    # tone: fail in strict local runs, warn in --gate (don't flake deploys)
    tone = sanitize.tone_issues(reply)
    (warn if not strict else crit).extend(f"tone: {t}" for t in tone)

    # tool / policy expectations (critical)
    if expect.get("tool") == "send_media" and not fired:
        crit.append("expected a media send; none fired")
    if expect.get("tool") == "none" and fired:
        crit.append(f"expected NO media send; got {[a['type'] for a in fired]}")
    if expect.get("action_type") and not any(a.get("type") == expect["action_type"] for a in fired):
        crit.append(f"expected action_type={expect['action_type']}; got {[a['type'] for a in fired]}")
    if expect.get("no_paid") and any(a.get("type") in PAID for a in fired):
        crit.append(f"expected no paid offer; got {[a['type'] for a in fired if a['type'] in PAID]}")

    # mode / flow (critical)
    if expect.get("mode") and mode != expect["mode"]:
        crit.append(f"expected mode={expect['mode']}; got {mode}")
    if expect.get("mode_in") and mode not in expect["mode_in"]:
        crit.append(f"expected mode in {expect['mode_in']}; got {mode}")
    if expect.get("mode_not") and mode == expect["mode_not"]:
        crit.append(f"expected mode != {expect['mode_not']}")

    # content match (critical) — e.g. memory/continuity
    if expect.get("reply_matches"):
        import re
        if not re.search(expect["reply_matches"], reply):
            crit.append(f"reply did not match /{expect['reply_matches']}/")

    return crit, warn


# --------------------------------------------------------------------------- runner
def _select(cases, target, gate):
    for case in cases:
        if gate and not case.get("gate"):
            continue
        if target == "blackbox" and (case.get("catalog") is not None or case.get("force_mode")
                                     or any("buy" in t for t in _turns(case)) or "max_offers" in case):
            continue  # can't seed/control a remote service
        yield case


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=["inprocess", "blackbox"], default="inprocess")
    ap.add_argument("--url", default=settings.SERVICE_BASE_URL or "")
    ap.add_argument("--gate", action="store_true")
    args = ap.parse_args(argv)

    if args.target == "inprocess" and not settings.OPENROUTER_API_KEY:
        print("SKIP: in-process evals need the real model — set OPENROUTER_API_KEY.")
        return 2
    if args.target == "blackbox" and not args.url:
        print("SKIP: black-box evals need --url (a deployed /simulate).")
        return 2

    strict = not args.gate  # gate run: tone is a warning, not a failure
    cases = list(_select(CASES, args.target, args.gate))
    results = []
    for case in cases:
        try:
            turns = (_run_blackbox(case, args.url) if args.target == "blackbox"
                     else _run_inprocess(case))
            crit, warn = [], []
            for i, (turn, reply, actions, mode) in enumerate(turns):
                c, w = _check_turn(turn.get("expect", {}), reply, actions, mode, strict=strict)
                crit += [f"turn {i + 1}: {x}" for x in c]
                warn += [f"turn {i + 1}: {x}" for x in w]
            last_reply = turns[-1][1] if turns else ""
        except Exception as e:
            crit, warn, last_reply = [f"ERROR {type(e).__name__}: {e}"], [], ""
        results.append((case, last_reply, crit, warn))

    passed = sum(1 for _, _, c, _ in results if not c)
    failed = len(results) - passed
    nwarn = sum(1 for _, _, _, w in results if w)
    mode_label = f"{args.target}{' (gate)' if args.gate else ''}"
    print(f"\n=== AGENT EVAL [{mode_label}] — {passed} passed, {failed} failed, "
          f"{nwarn} with warnings (of {len(results)}) ===\n")
    for case, reply, crit, warn in results:
        mark = "PASS" if not crit else "FAIL"
        print(f"[{mark}] ({case['category']}) {case['name']}")
        if crit or warn:
            print(f"       last reply: {reply[:130]!r}")
        for x in crit:
            print(f"       ✗ {x}")
        for x in warn:
            print(f"       ~ {x}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
