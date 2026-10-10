"""LLM seams — both injectable so tests can run offline/deterministic.

- Voice model  (Grok 4.3): a LangChain chat model used by the agent node.
- Classifier   (Mistral-Nemo): a plain JSON call used by classify_mode.
"""
from __future__ import annotations

import json

import httpx

from config import settings

_voice = None  # cached LangChain chat model (or a fake, in tests)


def get_voice_model():
    """Return the voice chat model, creating it lazily. Tests override via set_voice_model()."""
    global _voice
    if _voice is None:
        from langchain_openai import ChatOpenAI
        _voice = ChatOpenAI(
            model=settings.MODEL,
            base_url=settings.OPENROUTER_BASE_URL,
            api_key=settings.OPENROUTER_API_KEY,
            temperature=settings.TEMPERATURE,
            max_tokens=settings.MAX_OUTPUT_TOKENS,  # cap: chat replies are short + avoids over-reserving credits
        )
    return _voice


def set_voice_model(model) -> None:
    """Inject a fake/stub voice model (used by tests)."""
    global _voice
    _voice = model


def complete(system: str, user: str, model: str | None = None, max_tokens: int = 220) -> str:
    """One plain-text completion (used for the rolling conversation summary).

    Uses the cheap classifier model by default. Tests monkeypatch this to avoid
    the network.
    """
    body = {
        "model": model or settings.MODE_MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "max_tokens": max_tokens, "temperature": 0.3,
    }
    r = httpx.post(f"{settings.OPENROUTER_BASE_URL}/chat/completions", json=body,
                   headers={"Authorization": f"Bearer {settings.OPENROUTER_API_KEY}"}, timeout=30)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def classify_json(system: str, user: str, model: str | None = None) -> dict:
    """One JSON classification call (non-reasoning model). Returns parsed dict.

    Tests monkeypatch this to avoid the network.
    """
    body = {
        "model": model or settings.MODE_MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "max_tokens": 150, "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    r = httpx.post(f"{settings.OPENROUTER_BASE_URL}/chat/completions", json=body,
                   headers={"Authorization": f"Bearer {settings.OPENROUTER_API_KEY}"}, timeout=30)
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    return json.loads(content)
