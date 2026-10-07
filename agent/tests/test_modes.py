import llm
import modes


def test_normalize_rejects_unknown_mode():
    out = modes.normalize({"mode": "BANANA"}, "WARM")
    assert out["mode"] == "WARM"


def test_normalize_passes_valid_and_coerces_bools():
    out = modes.normalize({"mode": "flirty", "escalate_ok": 1, "monetize_ok": 0}, "COLD")
    assert out["mode"] == "FLIRTY"
    assert out["escalate_ok"] is True and out["monetize_ok"] is False


def test_classify_uses_model_output(monkeypatch):
    monkeypatch.setattr(llm, "classify_json",
                        lambda s, u, model=None: {"mode": "EXPLICIT", "monetize_ok": True})
    out = modes.classify([{"role": "fan", "text": "i want you"}], "FLIRTY")
    assert out["mode"] == "EXPLICIT" and out["monetize_ok"] is True


def test_classify_holds_mode_on_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(llm, "classify_json", boom)
    out = modes.classify([{"role": "fan", "text": "hi"}], "WARM")
    assert out["mode"] == "WARM"  # safe fallback, never crashes a reply
