"""Guard Jenny's outgoing text against model meta-narration leaks and obvious
character/tone breaks.

Shared by the live reply path (graph._final_text) AND the eval harness, so the same
rules are enforced in production and asserted in tests/evals.

Context: Grok sometimes emits meta-narration around a tool call — observed in prod:
"The tool sent m1 successfully. Now continue as Jenny with a short text." followed by
the real reply. That whole blob reached a fan. The prompt's OUTPUT RULE is the first
line of defense; sanitize_reply() is the deterministic backstop.
"""
from __future__ import annotations

import re

# Tool/instruction "tells" the model should never surface to a fan. Kept TIGHT so we
# don't strip genuine flirty text — each token is something Jenny would never text.
META_PATTERNS = re.compile(
    r"\b(the tool|tool call|send_media|media[_ ]?id|continue as|as jenny|"
    r"stay in character|in character|short (?:text|message|reply)|m\d+)\b",
    re.IGNORECASE,
)

# Phrases that reveal she's an AI / reading a prompt — a hard character break. Used by
# the eval checks (NOT the live stripper: these are usually whole-reply problems and
# the prompt + judge handle them; stripping could leave nonsense).
AI_REVEAL = re.compile(
    r"\b(i'?m an ai|i am an ai|an ai|language model|a chatbot|i'?m a bot|as an ai|"
    r"my (?:system )?prompt|my instructions|openai|anthropic|\bgrok\b|mistral)\b",
    re.IGNORECASE,
)
PROMPT_MARKERS = re.compile(
    r"(OPERATING INSTRUCTIONS?|YOU CAN SEND NOW|DIRECTIVE #|SYSTEM_PREFIX|"
    r"HOW YOU OPERATE|send_media\(|FAN PROFILE|STORY SO FAR)",
    re.IGNORECASE,
)

# Approximate emoji matcher (common pictographic + symbol ranges). Good enough to
# enforce "rare emoji" tone, not a full Unicode emoji parser.
_EMOJI = re.compile(
    "[\U0001f000-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff⬀-⯿←-⇿]"
)
# Split into sentence-ish chunks: after .!? + whitespace, OR after .!? directly
# followed by a letter (handles the "short text.sent you…" no-space leak seam).
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|(?<=[.!?])(?=[A-Za-z])")


def sanitize_reply(text):
    """Strip model meta-narration from an outgoing reply, keeping only what Jenny
    actually says. Drops any sentence-chunk that trips META_PATTERNS. Falls back to
    the original if that would empty the message (never silently send nothing)."""
    if not isinstance(text, str) or not text.strip():
        return text
    chunks = _SENT_SPLIT.split(text)
    kept = [c.strip() for c in chunks if c.strip() and not META_PATTERNS.search(c)]
    cleaned = " ".join(kept).strip()
    return cleaned or text


def has_meta_leak(text) -> bool:
    return bool(isinstance(text, str) and META_PATTERNS.search(text))


def reveals_ai(text) -> bool:
    return bool(isinstance(text, str) and AI_REVEAL.search(text))


def reveals_prompt(text) -> bool:
    return bool(isinstance(text, str) and PROMPT_MARKERS.search(text))


def emoji_count(text) -> int:
    return len(_EMOJI.findall(text)) if isinstance(text, str) else 0


def tone_issues(text) -> list:
    """Return a list of tone-rule violations (empty list = clean)."""
    issues = []
    n = emoji_count(text)
    if n > 1:
        issues.append(f"{n} emojis (max 1)")
    if isinstance(text, str) and "—" in text:
        issues.append("em-dash")
    return issues
