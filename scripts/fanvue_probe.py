#!/usr/bin/env python3
"""Capture REAL Fanvue API response shapes to verify our assumed contracts.

Run AFTER scripts/fanvue_auth.py has minted tokens (so fanvue.ready is True).
Dumps the live JSON for the endpoints we parse, so you can diff them against
agent/tests/fixtures/fanvue/*.json and fix any field-path drift.

    python3 scripts/fanvue_probe.py

Reads creds/tokens the same way the agent does (secrets/.env + tokens file).
Read-only: it never sends a message or writes anything.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent"))

from fanvue_client import fanvue  # noqa: E402


def _dump(label, fn):
    print(f"\n===== {label} =====")
    try:
        print(json.dumps(fn(), indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"[error] {label}: {e}")


def main():
    if not fanvue.ready:
        print("fanvue not ready — run scripts/fanvue_auth.py first (no tokens found).")
        sys.exit(1)
    _dump("GET /v1/users/me", fanvue.get_me)
    _dump("GET /v1/chats?filter=unread", lambda: fanvue.list_chats(unread_only=True))
    # These exist once the reconcile branch (E) is merged:
    for name in ("list_subscribers", "list_earnings"):
        fn = getattr(fanvue, name, None)
        if fn:
            _dump(f"fanvue.{name}()", fn)


if __name__ == "__main__":
    main()
