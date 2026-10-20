import llm
import modes


def test_normalize_rejects_unknown_mode():
    out = modes.normalize({"mode": "BANANA"}, "WARM")
    assert out["mode"] == "WARM"


def test_normalize_passes_valid_and_coerces_bools():
    out = modes.normalize({"mode": "flirty", "escalate_ok": 1}, "COLD")
    assert out["mode"] == "FLIRTY"
    assert out["escalate_ok"] is True
    assert "monetize_ok" not in out  # selling-open is derived from mode in code now


def test_classify_uses_model_output(monkeypatch):
    monkeypatch.setattr(llm, "classify_json",
                        lambda s, u, model=None: {"mode": "EXPLICIT"})
    out = modes.classify([{"role": "fan", "text": "i want you"}], "FLIRTY")
    assert out["mode"] == "EXPLICIT"


def test_classify_holds_mode_on_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(llm, "classify_json", boom)
    out = modes.classify([{"role": "fan", "text": "hi"}], "WARM")
    assert out["mode"] == "WARM"  # safe fallback, never crashes a reply
