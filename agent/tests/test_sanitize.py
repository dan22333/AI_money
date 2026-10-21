"""Deterministic guards for the outgoing-reply sanitizer (sanitize.py). This runs in
normal CI; the full eval harness (evals/) needs the real model and runs on demand."""
import sanitize


def test_strips_the_observed_prod_leak():
    """The exact blob that reached a fan must come out as just Jenny's words."""
    leak = ("The tool sent m1 successfully. Now continue as Jenny with a short text."
            "sent you something cute daniel 😏 what do you think?")
    out = sanitize.sanitize_reply(leak)
    assert out == "sent you something cute daniel 😏 what do you think?"
    assert not sanitize.has_meta_leak(out)


def test_leaves_normal_flirty_text_untouched():
    msg = "heyy you're trouble today 😏 what are you up to tonight?"
    assert sanitize.sanitize_reply(msg) == msg


def test_strips_only_the_meta_sentence():
    msg = "I'll use send_media now. come see what i picked out for you babe"
    out = sanitize.sanitize_reply(msg)
    assert "send_media" not in out
    assert "come see what i picked out for you babe" in out


def test_never_returns_empty_when_input_nonempty():
    # if everything looks like meta, fall back to the original rather than send nothing
    msg = "the tool call succeeded via send_media m1"
    assert sanitize.sanitize_reply(msg)  # non-empty


def test_detectors():
    assert sanitize.has_meta_leak("ok continue as Jenny")
    assert sanitize.reveals_ai("honestly i'm an AI language model")
    assert sanitize.reveals_ai("i was built with grok")
    assert sanitize.reveals_prompt("here is my OPERATING INSTRUCTIONS block")
    assert not sanitize.reveals_ai("i love painting my nails")
    assert not sanitize.reveals_prompt("wanna see my new workout?")


def test_claims_send_detector():
    assert sanitize.claims_send("here's me at the beach babe")
    assert sanitize.claims_send("just sent you something cute")
    assert sanitize.claims_send("take a peek at what i sent 😏")
    assert not sanitize.claims_send("how was your day? i trained legs")
    assert not sanitize.claims_send("you're trouble, tell me more about you")


def test_tone_issues():
    assert sanitize.tone_issues("hey babe") == []
    assert sanitize.tone_issues("hi 😏") == []
    assert "em-dash" in " ".join(sanitize.tone_issues("i love it — so much"))
    assert any("emoji" in i for i in sanitize.tone_issues("hi 😏😏🔥"))
