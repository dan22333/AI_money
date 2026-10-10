"""Thin Fanvue API client with OAuth token management.

Token strategy (see docs/auth):
  - A one-time browser authorization (fanvue_auth.py) produces a REFRESH TOKEN
    (requires offline_access scope).
  - This client trades the refresh token for a ~1h access token.
  - Refresh tokens are SINGLE-USE and rotate: every refresh returns a new one,
    which we persist immediately. Only ONE process should refresh at a time.

Token persistence has two backends, chosen by settings.FANVUE_TOKENS_SOURCE:
  - "file"   — local dev: a JSON file (secrets/.fanvue_tokens.json).
  - "secret" — on GCP: the token JSON lives in a Secret Manager secret
    (settings.FANVUE_TOKENS_SECRET); a rotation writes a NEW secret version.
    The runtime SA needs secretAccessor (read) and secretVersionAdder (write).
If no token is present (e.g. the secret isn't created yet) `ready` is False and
the agent just computes replies without sending — it never crashes.
"""
from __future__ import annotations

import base64
import json
import time
from typing import List, Optional

import httpx

from config import settings


class FanvueClient:
    def __init__(self):
        self._tokens: dict = {}
        self._access_expiry: float = 0.0
        self._load_tokens()

    # ---- token persistence (file for dev, Secret Manager on GCP) ----
    def _use_secret(self) -> bool:
        return settings.FANVUE_TOKENS_SOURCE == "secret"

    def _secret_name(self) -> str:
        return f"projects/{settings.GCP_PROJECT}/secrets/{settings.FANVUE_TOKENS_SECRET}"

    def _load_tokens(self):
        try:
            if self._use_secret():
                from google.cloud import secretmanager
                client = secretmanager.SecretManagerServiceClient()
                resp = client.access_secret_version(name=f"{self._secret_name()}/versions/latest")
                payload = resp.payload.data.decode().strip()
                self._tokens = json.loads(payload) if payload else {}
            else:
                with open(settings.FANVUE_TOKENS_FILE) as f:
                    self._tokens = json.load(f)
        except Exception:
            # missing/empty/unreadable → unconfigured; stay not-ready, never crash
            self._tokens = {}

    def _save_tokens(self):
        if self._use_secret():
            from google.cloud import secretmanager
            client = secretmanager.SecretManagerServiceClient()
            client.add_secret_version(
                parent=self._secret_name(),
                payload={"data": json.dumps(self._tokens).encode()},
            )
        else:
            with open(settings.FANVUE_TOKENS_FILE, "w") as f:
                json.dump(self._tokens, f, indent=2)

    @property
    def ready(self) -> bool:
        return bool(self._tokens.get("refresh_token") or self._tokens.get("access_token"))

    # ---- access token lifecycle ----
    def _refresh(self):
        refresh_token = self._tokens.get("refresh_token")
        if not refresh_token:
            raise RuntimeError("No refresh token — run fanvue_auth.py first.")
        basic = base64.b64encode(
            f"{settings.OAUTH_CLIENT_ID}:{settings.OAUTH_CLIENT_SECRET}".encode()
        ).decode()
        r = httpx.post(settings.OAUTH_TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }, headers={"Authorization": f"Basic {basic}",
                    "Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
        r.raise_for_status()
        tok = r.json()
        # rotation: persist the NEW refresh token right away
        self._tokens.update(tok)
        self._access_expiry = time.time() + int(tok.get("expires_in", 3600)) - 60
        self._save_tokens()

    def _access_token(self) -> str:
        if not self._tokens.get("access_token") or time.time() >= self._access_expiry:
            self._refresh()
        return self._tokens["access_token"]

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._access_token()}",
                "Content-Type": "application/json"}

    # ---- API surface (expand as needed) ----
    def get_me(self) -> dict:
        r = httpx.get(f"{settings.FANVUE_API_BASE}/v1/users/me", headers=self._headers(), timeout=30)
        r.raise_for_status()
        return r.json()

    def list_chats(self, unread_only: bool = True) -> list:
        params = {"filter": "unread"} if unread_only else {}
        r = httpx.get(f"{settings.FANVUE_API_BASE}/v1/chats", headers=self._headers(),
                      params=params, timeout=30)
        r.raise_for_status()
        return r.json().get("data", [])

    def send_message(self, user_uuid: str, text: str,
                     media_uuids: Optional[List[str]] = None,
                     price_cents: Optional[int] = None) -> dict:
        body: dict = {"text": text}
        if media_uuids:
            body["mediaUuids"] = media_uuids
        if price_cents:
            body["price"] = price_cents  # cents; makes it pay-to-view
        r = httpx.post(f"{settings.FANVUE_API_BASE}/v1/chats/{user_uuid}/message",
                       headers=self._headers(), json=body, timeout=30)
        r.raise_for_status()
        return r.json()


# single shared instance
fanvue = FanvueClient()
