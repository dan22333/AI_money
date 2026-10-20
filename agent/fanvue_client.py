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
    def __init__(self, creator_id: Optional[str] = None):
        self._creator_id = creator_id or settings.FANVUE_CREATOR_ID
        self._tokens: dict = {}
        self._access_expiry: float = 0.0
        self._load_tokens()

    # ---- token persistence: file (dev) | secret (Secret Manager) | firestore ----
    def _source(self) -> str:
        return settings.FANVUE_TOKENS_SOURCE

    def _use_secret(self) -> bool:
        return self._source() == "secret"

    def _is_distributed(self) -> bool:
        """Backends shared by many Cloud Run instances → a refresh must be
        serialized by the cross-instance lock. File (single process) need not."""
        return self._source() in ("secret", "firestore")

    def _secret_name(self) -> str:
        return f"projects/{settings.GCP_PROJECT}/secrets/{settings.FANVUE_TOKENS_SECRET}"

    def _read_secret(self) -> dict:
        """Latest Secret Manager version as a dict ({} if missing/empty/unreadable)."""
        try:
            from google.cloud import secretmanager
            client = secretmanager.SecretManagerServiceClient()
            resp = client.access_secret_version(name=f"{self._secret_name()}/versions/latest")
            payload = resp.payload.data.decode().strip()
            return json.loads(payload) if payload else {}
        except Exception:
            return {}

    def _load_tokens(self):
        try:
            src = self._source()
            if src == "firestore":
                import store
                with store.namespace("real"):
                    doc = store.get_fanvue_token(self._creator_id)
                    if not doc:
                        # one-time migration: seed the doc from the old Secret
                        # Manager token so flipping the source needs no manual copy.
                        doc = self._read_secret()
                        if doc:
                            doc = {**doc, "creatorId": self._creator_id}
                            store.set_fanvue_token(self._creator_id, doc)
                self._tokens = doc or {}
            elif src == "secret":
                self._tokens = self._read_secret()
            else:
                with open(settings.FANVUE_TOKENS_FILE) as f:
                    self._tokens = json.load(f)
        except Exception:
            # missing/empty/unreadable → unconfigured; stay not-ready, never crash
            self._tokens = {}
        # Restore the ABSOLUTE access-token expiry from the persisted token so a
        # freshly-booted (or re-reading) instance knows whether a sibling's access
        # token is still valid, and won't needlessly rotate the single-use refresh
        # token. 0.0 (unknown) → treated as expired, forcing a refresh.
        self._access_expiry = float(self._tokens.get("access_expires_at") or 0.0)

    def _save_tokens(self):
        src = self._source()
        if src == "firestore":
            import store
            self._tokens["creatorId"] = self._creator_id
            with store.namespace("real"):
                store.set_fanvue_token(self._creator_id, self._tokens)
        elif src == "secret":
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
    @property
    def _refresh_lock_name(self) -> str:
        # Per-creator: refreshing creator A's token must not block creator B's, but
        # two refreshes of the SAME creator can never race (single-use token).
        return f"fanvue_token_refresh:{self._creator_id}"

    def _token_request(self, refresh_token: str) -> dict:
        """POST the refresh grant to Fanvue and return the new token JSON. Raises
        httpx.HTTPStatusError on a non-2xx (400 invalid_grant = token is dead)."""
        basic = base64.b64encode(
            f"{settings.OAUTH_CLIENT_ID}:{settings.OAUTH_CLIENT_SECRET}".encode()
        ).decode()
        r = httpx.post(settings.OAUTH_TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }, headers={"Authorization": f"Basic {basic}",
                    "Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
        r.raise_for_status()
        return r.json()

    def _apply_and_save(self, tok: dict):
        """Adopt a fresh token response (rotates the refresh token) and persist it,
        stamping the ABSOLUTE access expiry so every instance can later tell whether
        this access token is still usable without refreshing again."""
        self._tokens.update(tok)
        self._access_expiry = time.time() + int(tok.get("expires_in", 3600)) - 60
        self._tokens["access_expires_at"] = self._access_expiry
        self._save_tokens()

    def _refresh(self):
        """Trade the refresh token for a new access token.

        File backend (local dev) = a single process, so just do it. Distributed
        backends (secret / firestore on GCP, many Cloud Run instances) = refresh
        under a cross-instance lock, because Fanvue refresh tokens are single-use +
        rotating: two instances refreshing at once would invalidate each other's
        token and lock out the whole fleet. Inside the lock we RE-READ the latest
        persisted token first — if a sibling already refreshed, we reuse its
        still-valid access token instead of rotating again."""
        if not self._is_distributed():
            refresh_token = self._tokens.get("refresh_token")
            if not refresh_token:
                raise RuntimeError("No refresh token — run fanvue_auth.py first.")
            self._apply_and_save(self._token_request(refresh_token))
            return
        self._refresh_coordinated()

    def _refresh_coordinated(self):
        import uuid

        import store
        owner = uuid.uuid4().hex
        with store.namespace("real"):  # one lock for the whole fleet, not per-DB
            acquired = False
            for _ in range(settings.FANVUE_REFRESH_LOCK_TRIES):
                acquired = store.acquire_lock(
                    self._refresh_lock_name, owner, settings.FANVUE_REFRESH_LOCK_TTL)
                if acquired:
                    break
                # Another instance is refreshing. Wait, then re-read what it persists;
                # the moment a valid access token lands we can stop waiting.
                time.sleep(settings.FANVUE_REFRESH_LOCK_WAIT)
                self._load_tokens()
                if self._tokens.get("access_token") and time.time() < self._access_expiry:
                    return
            if not acquired:
                # Couldn't win the lock in time. Fall back to the latest persisted
                # token; raise only if there's genuinely nothing usable.
                self._load_tokens()
                if not self._tokens.get("access_token"):
                    raise RuntimeError("Fanvue token refresh is busy and no cached access token is available.")
                return
            try:
                # Inside the lock: re-read the latest token. A sibling may have
                # rotated it between our stale read and acquiring the lock.
                self._load_tokens()
                if self._tokens.get("access_token") and time.time() < self._access_expiry:
                    return  # sibling already refreshed — reuse, don't rotate again
                refresh_token = self._tokens.get("refresh_token")
                if not refresh_token:
                    raise RuntimeError("No refresh token — run fanvue_auth.py first.")
                try:
                    tok = self._token_request(refresh_token)
                except httpx.HTTPStatusError as e:
                    # 400/401 can mean our refresh token lost a rotation race. Reload
                    # the latest persisted token ONCE and retry with it before failing.
                    if e.response is not None and e.response.status_code in (400, 401):
                        self._load_tokens()
                        latest = self._tokens.get("refresh_token")
                        if latest and latest != refresh_token:
                            tok = self._token_request(latest)
                        else:
                            raise
                    else:
                        raise
                self._apply_and_save(tok)
            finally:
                store.release_lock(self._refresh_lock_name, owner)

    def _access_token(self) -> str:
        if not self._tokens.get("access_token") or time.time() >= self._access_expiry:
            self._refresh()
        return self._tokens["access_token"]

    def ensure_fresh(self, skew_seconds: float) -> str:
        """Proactive refresh: rotate the token now if it's missing or within
        `skew_seconds` of expiry, otherwise leave it. Returns a status string
        ('refreshed' | 'fresh' | 'unconfigured'). Driven by the Cloud Scheduler
        cron (POST /admin/refresh_tokens) so the token never expires during quiet
        periods and no user-facing request pays the refresh latency. Idempotent
        and lock-guarded — safe to call on every cron tick and from many instances."""
        self._load_tokens()
        if not self._tokens.get("refresh_token"):
            return "unconfigured"
        if self._tokens.get("access_token") and time.time() < self._access_expiry - skew_seconds:
            return "fresh"
        self._refresh()
        return "refreshed"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._access_token()}",
                "Content-Type": "application/json"}

    # ---- API surface (expand as needed) ----
    def get_me(self) -> dict:
        r = httpx.get(f"{settings.FANVUE_API_BASE}/v1/users/me", headers=self._headers(), timeout=30)
        r.raise_for_status()
        return r.json()

    def _paginate(self, path: str, params: Optional[dict] = None, *, max_pages: int = 500) -> list:
        """Follow Fanvue cursor pagination to return the FULL list, not just page 1.

        Fanvue's list contract (official docs): request params `cursor` (opaque,
        from the previous response's `nextCursor`) + `size` (default 15, max 50);
        response `{data, nextCursor, total}` with `nextCursor=null` at the end.
        We request size=50 to minimise round-trips. Guards: stop on a falsy or
        REPEATED cursor (so a server that ignores `cursor` can't loop forever) and
        a hard page cap. Without this, every list method silently truncated at the
        first 15 rows."""
        params = dict(params or {})
        params.setdefault("size", 50)
        out: list = []
        cursor: Optional[str] = None
        seen: set = set()
        for _ in range(max_pages):
            q = dict(params)
            if cursor:
                q["cursor"] = cursor
            r = httpx.get(f"{settings.FANVUE_API_BASE}{path}", headers=self._headers(),
                          params=q, timeout=30)
            r.raise_for_status()
            body = r.json()
            out.extend(body.get("data") or [])
            cursor = body.get("nextCursor")
            if not cursor or cursor in seen:
                break
            seen.add(cursor)
        return out

    def list_chats(self, unread_only: bool = True) -> list:
        return self._paginate("/v1/chats", {"filter": "unread"} if unread_only else {})

    def list_subscribers(self) -> list:
        """Active subscribers. Used by the nightly reconcile to backfill fans."""
        return self._paginate("/v1/subscribers", {"status": "active"})

    def list_followers(self) -> list:
        """Followers (free fans). Verified live: GET /v1/followers → {data:[{uuid,
        handle, displayName, nickname, isTopSpender, registeredAt}], nextCursor}."""
        return self._paginate("/v1/followers")

    def list_earnings(self, since: str | None = None) -> list:
        """Earnings/invoices. Verified live: GET /v1/insights/earnings → {data:[{date,
        gross, net, total, currency, source, transactionOrderId, transactionOrderStatus,
        user:{uuid,handle}}], nextCursor}. `since` filters via startDate (delta sync)."""
        return self._paginate("/v1/insights/earnings", {"startDate": since} if since else {})

    def list_media(self) -> list:
        """Creator's own media library (catalog). Verified live: GET /v1/media →
        {data:[{uuid, mediaType, recommendedPrice, caption, description, name, tags,
        status, variants}]}. Needs read:media."""
        return self._paginate("/v1/media")

    def list_chat_messages(self, user_uuid: str, since: str | None = None) -> list:
        """Full message history for one chat (both directions). Verified live:
        GET /v1/chats/{userUuid}/messages → {data:[{uuid, text, sender, recipient,
        sentAt, type, status, ...}], dateFilter}. `since` supports delta sync."""
        return self._paginate(f"/v1/chats/{user_uuid}/messages",
                              {"startDate": since} if since else {})

    def raw_get(self, path: str, params: Optional[dict] = None) -> tuple[int, object]:
        """Authenticated GET that does NOT raise — returns (status_code, parsed_body).
        Used by the admin probe to discover real API shapes without a 500."""
        r = httpx.get(f"{settings.FANVUE_API_BASE}{path}", headers=self._headers(),
                      params=params or {}, timeout=30)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {"_text": r.text[:500]}

    def raw_post(self, path: str, json_body: Optional[dict] = None) -> tuple[int, object]:
        """Authenticated POST that does NOT raise — returns (status_code, body).
        Used by the admin probe to discover the real send-message endpoint."""
        r = httpx.post(f"{settings.FANVUE_API_BASE}{path}", headers=self._headers(),
                       json=json_body or {}, timeout=30)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {"_text": r.text[:500]}

    # Fanvue's minimum pay-to-view price (cents). The API rejects a lower non-zero
    # price with a 400, so we clamp up to it rather than let a send fail.
    PPV_MIN_PRICE_CENTS = 300

    def send_message(self, user_uuid: str, text: str,
                     media_uuids: Optional[List[str]] = None,
                     price_cents: Optional[int] = None) -> dict:
        """POST a chat message. Body contract (verified against Fanvue API docs
        2026-10-20, api.fanvue.com/docs/api-reference/send-a-message):
          {text, mediaUuids:[uuid], price:cents}
        - the media field is `mediaUuids` (an ARRAY), NOT `media` — the old `media`
          key was silently ignored, so attachments never reached the fan.
        - `price` (cents) makes the media pay-to-view; minimum 300 — a lower non-zero
          price 400s, so we clamp. Price requires mediaUuids.
        - the attached media must already be status:"ready" on Fanvue.
        Endpoint is SINGULAR /message (plural /messages is GET-only → 404 on POST)."""
        body: dict = {"text": text}
        if media_uuids:
            body["mediaUuids"] = media_uuids  # ARRAY; Fanvue's field is `mediaUuids`
            if price_cents:
                body["price"] = max(int(price_cents), self.PPV_MIN_PRICE_CENTS)
        r = httpx.post(f"{settings.FANVUE_API_BASE}/v1/chats/{user_uuid}/message",
                       headers=self._headers(), json=body, timeout=30)
        r.raise_for_status()
        return r.json()


# single shared instance (the configured creator)
fanvue = FanvueClient()


def refresh_all(skew_seconds: Optional[float] = None) -> dict:
    """Proactively refresh every creator's token that's within `skew_seconds` of
    expiry. Driven by the Cloud Scheduler cron via POST /admin/refresh_tokens.

    Enumerates creators from the Firestore token collection (multi-creator ready);
    for the file/secret backends there's just the one configured creator. Reuses
    the global client for the configured creator so a warm token isn't re-read.
    Never raises on a single creator's failure — reports it per-creator."""
    skew = settings.FANVUE_REFRESH_SKEW_SECONDS if skew_seconds is None else skew_seconds
    creator_ids: List[str] = []
    if settings.FANVUE_TOKENS_SOURCE == "firestore":
        import store
        with store.namespace("real"):
            creator_ids = store.list_fanvue_token_creator_ids()
    if settings.FANVUE_CREATOR_ID not in creator_ids:
        creator_ids.append(settings.FANVUE_CREATOR_ID)

    results: dict = {"refreshed": 0, "fresh": 0, "unconfigured": 0, "errors": 0, "creators": {}}
    for cid in creator_ids:
        client = fanvue if cid == fanvue._creator_id else FanvueClient(cid)
        try:
            status = client.ensure_fresh(skew)
        except Exception as e:  # one creator's failure must not stop the rest
            status = "error"
            print(f"[refresh_all] creator={cid} failed ({type(e).__name__}: {e})")
        results["creators"][cid] = status
        results[status if status in ("refreshed", "fresh", "unconfigured") else "errors"] += 1
    return results
