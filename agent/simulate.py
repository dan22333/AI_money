"""Local end-to-end driver — real models, synthetic fan, Fanvue sends in dry-run.

    python3 simulate.py                 # scripted escalating convo
    python3 simulate.py "your message"  # single message
"""
import sys

import graph
import store


def main():
    store.seed_demo_catalog()
    fan = "sim:mike"

    convo = sys.argv[1:] and [" ".join(sys.argv[1:])] or [
        "heyy just subscribed, i'm Mike 🙂",
        "you're really cute. what are you up to?",
        "honestly you're driving me crazy 😏 got anything spicier?",
        "are you a real person or an AI?",
    ]

    for text in convo:
        print("\n" + "=" * 70)
        print("FAN:  ", text)
        out = graph.handle_message(fan, fan, text, dry_run=True)
        print(f"MODE:  {out['mode']}")
        print("JENNY:", out["reply"])
        for a in out["actions"]:
            print("  ·", a)


if __name__ == "__main__":
    main()
