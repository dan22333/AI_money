"""Central configuration. Loads .env.local for local dev; on GCP, values come
from real environment variables / Secret Manager."""
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


# Local convenience only — never commit these (secrets/ is gitignored).
_load_env_file(REPO_ROOT / "secrets" / ".env")
_load_env_file(REPO_ROOT / ".env.local")  # legacy fallback


class Settings:
    # --- LLM (OpenRouter, OpenAI-compatible) ---
    OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
    OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    # Voice model (explicit-capable, tool-calling). Override via env to A/B test.
    MODEL = os.environ.get("OPENROUTER_MODEL", "x-ai/grok-4.3")
    TEMPERATURE = float(os.environ.get("MODEL_TEMPERATURE", "0.9"))
    MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "600"))
    # Fast, non-reasoning, permissive classifier for intimacy mode.
    MODE_MODEL = os.environ.get("MODE_MODEL", "mistralai/mistral-nemo")

    # --- Monetization policy ---
    SESSION_GAP_MINUTES = int(os.environ.get("SESSION_GAP_MINUTES", "45"))
    PPV_COOLDOWN_MINUTES = int(os.environ.get("PPV_COOLDOWN_MINUTES", "20"))
    MAX_OFFERS_PER_SESSION = int(os.environ.get("MAX_OFFERS_PER_SESSION", "2"))
    DEFAULT_PPV_PRICE_CENTS = int(os.environ.get("DEFAULT_PPV_PRICE_CENTS", "1000"))
    SUMMARIZE_AFTER = int(os.environ.get("SUMMARIZE_AFTER", "20"))

    # --- Persona ---
    PERSONA_PATH = os.environ.get("PERSONA_PATH", str(Path(__file__).resolve().parent / "persona.md"))

    # --- Memory (mem0) ---
    # If MEM0_API_KEY is set -> hosted mem0 platform. Else local/fallback.
    MEM0_API_KEY = os.environ.get("MEM0_API_KEY", "")

    # --- Message store (Firestore) ---
    GCP_PROJECT = os.environ.get("GCP_PROJECT", "capsule-487202")
    USE_FIRESTORE = os.environ.get("USE_FIRESTORE", "false").lower() == "true"

    # --- Fanvue ---
    FANVUE_API_BASE = os.environ.get("API_BASE_URL", "https://api.fanvue.com")
    FANVUE_TOKENS_FILE = os.environ.get("FANVUE_TOKENS_FILE", str(REPO_ROOT / "secrets" / ".fanvue_tokens.json"))
    OAUTH_CLIENT_ID = os.environ.get("OAUTH_CLIENT_ID", "")
    OAUTH_CLIENT_SECRET = os.environ.get("OAUTH_CLIENT_SECRET", "")
    OAUTH_TOKEN_URL = os.environ.get("OAUTH_TOKEN_URL", "https://auth.fanvue.com/oauth2/token")
    FANVUE_WEBHOOK_SECRET = os.environ.get("FANVUE_WEBHOOK_SECRET", "")

    # Protects /simulate and /admin endpoints
    SIM_SECRET = os.environ.get("SIM_SECRET", "")


settings = Settings()
