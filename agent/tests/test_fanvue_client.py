"""Fanvue OAuth token persistence — Secret Manager backend. All network and
Secret Manager calls are mocked; nothing hits a real API."""
import json
import sys
import types

import fanvue_client
from config import settings


def _fresh_client():
    """A FanvueClient without running __init__'s auto-load (we drive loads explicitly)."""
    c = fanvue_client.FanvueClient.__new__(fanvue_client.FanvueClient)
    c._tokens = {}
    c._access_expiry = 0.0
    return c


def _install_fake_secretmanager(monkeypatch, *, stored=None):
    """Install a fake google.cloud.secretmanager whose client records add_secret_version
    calls and returns `stored` (a dict or raw str) from access_secret_version."""
    calls = {"added": []}

    class FakeClient:
        def access_secret_version(self, name):
            if stored is None:
                raise RuntimeError("not found")
            data = stored if isinstance(stored, (bytes, str)) else json.dumps(stored)
            data = data.encode() if isinstance(data, str) else data
            payload = types.SimpleNamespace(data=data)
            return types.SimpleNamespace(payload=payload)

        def add_secret_version(self, parent, payload):
            calls["added"].append({"parent": parent, "data": payload["data"]})
            return types.SimpleNamespace(name=f"{parent}/versions/2")

    fake_mod = types.ModuleType("secretmanager")
    fake_mod.SecretManagerServiceClient = lambda *a, **k: FakeClient()
    # `from google.cloud import secretmanager` resolves google.cloud.secretmanager
    monkeypatch.setitem(sys.modules, "google.cloud.secretmanager", fake_mod)
    import google.cloud
    monkeypatch.setattr(google.cloud, "secretmanager", fake_mod, raising=False)
    return calls


def test_ready_false_when_no_tokens_file(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "FANVUE_TOKENS_SOURCE", "file")
    monkeypatch.setattr(settings, "FANVUE_TOKENS_FILE", str(tmp_path / "nope.json"))
    c = fanvue_client.FanvueClient()
    assert c.ready is False


def test_loads_tokens_from_secret(monkeypatch):
    monkeypatch.setattr(settings, "FANVUE_TOKENS_SOURCE", "secret")
    monkeypatch.setattr(settings, "GCP_PROJECT", "test-proj")
    monkeypatch.setattr(settings, "FANVUE_TOKENS_SECRET", "fanvue-oauth-tokens")
    _install_fake_secretmanager(monkeypatch, stored={"refresh_token": "r0", "access_token": "a0"})
    c = fanvue_client.FanvueClient()
    assert c.ready is True
    assert c._tokens["refresh_token"] == "r0"


def test_ready_false_when_secret_empty_or_missing(monkeypatch):
    monkeypatch.setattr(settings, "FANVUE_TOKENS_SOURCE", "secret")
    monkeypatch.setattr(settings, "GCP_PROJECT", "test-proj")
    # missing version → access raises → caught → not ready, no crash
    _install_fake_secretmanager(monkeypatch, stored=None)
    assert fanvue_client.FanvueClient().ready is False
    # present but empty payload → not ready
    _install_fake_secretmanager(monkeypatch, stored="")
    assert fanvue_client.FanvueClient().ready is False


def _client_with_token(monkeypatch):
    """A ready client whose _headers() won't trigger a network refresh."""
    c = _fresh_client()
    c._tokens = {"access_token": "a0"}
    c._access_expiry = float("inf")
    return c


def test_paginate_follows_cursor_across_pages(monkeypatch):
    """_paginate must return EVERY page, requesting size=50 and passing the
    previous nextCursor as `cursor`, stopping when nextCursor is null."""
    c = _client_with_token(monkeypatch)
    pages = {
        None: {"data": [{"uuid": "1"}, {"uuid": "2"}], "nextCursor": "c1"},
        "c1": {"data": [{"uuid": "3"}], "nextCursor": None},
    }
    seen_params = []

    def fake_get(url, headers=None, params=None, timeout=None):
        seen_params.append(params)
        body = pages[params.get("cursor")]
        return types.SimpleNamespace(json=lambda: body, raise_for_status=lambda: None)

    monkeypatch.setattr(fanvue_client.httpx, "get", fake_get)
    out = c.list_followers()
    assert [x["uuid"] for x in out] == ["1", "2", "3"]       # all pages concatenated
    assert all(p.get("size") == 50 for p in seen_params)     # max page size requested
    assert seen_params[1].get("cursor") == "c1"              # cursor threaded through


def test_paginate_stops_on_repeated_cursor(monkeypatch):
    """If the server ignores `cursor` and keeps returning the same nextCursor,
    the seen-cursor guard must break instead of looping forever."""
    c = _client_with_token(monkeypatch)
    calls = {"n": 0}

    def fake_get(url, headers=None, params=None, timeout=None):
        calls["n"] += 1
        body = {"data": [{"uuid": "x"}], "nextCursor": "STUCK"}  # never advances
        return types.SimpleNamespace(json=lambda: body, raise_for_status=lambda: None)

    monkeypatch.setattr(fanvue_client.httpx, "get", fake_get)
    out = c.list_media()
    assert calls["n"] == 2          # page 1, then page 2 returns the same cursor → stop
    assert len(out) == 2            # no infinite loop


def test_rotation_writes_new_secret_version(monkeypatch):
    monkeypatch.setattr(settings, "FANVUE_TOKENS_SOURCE", "secret")
    monkeypatch.setattr(settings, "GCP_PROJECT", "test-proj")
    monkeypatch.setattr(settings, "FANVUE_TOKENS_SECRET", "fanvue-oauth-tokens")
    monkeypatch.setattr(settings, "OAUTH_CLIENT_ID", "cid")
    monkeypatch.setattr(settings, "OAUTH_CLIENT_SECRET", "csecret")
    calls = _install_fake_secretmanager(monkeypatch, stored={"refresh_token": "r0"})

    # fake the token endpoint returning a ROTATED refresh token
    def fake_post(url, data=None, headers=None, timeout=None):
        return types.SimpleNamespace(
            json=lambda: {"access_token": "a1", "refresh_token": "r1", "expires_in": 3600},
            raise_for_status=lambda: None,
        )
    monkeypatch.setattr(fanvue_client.httpx, "post", fake_post)

    c = fanvue_client.FanvueClient()              # loads {"refresh_token": "r0"}
    token = c._access_token()                     # no access token yet → refresh
    assert token == "a1"
    assert c._tokens["refresh_token"] == "r1"     # rotated in memory
    assert len(calls["added"]) == 1               # persisted as a NEW secret version
    written = json.loads(calls["added"][0]["data"].decode())
    assert written["refresh_token"] == "r1" and written["access_token"] == "a1"
    assert written["access_expires_at"] > 0       # absolute expiry stamped for siblings


def test_reuses_sibling_token_instead_of_rotating(monkeypatch):
    """Multi-instance race guard: if this instance's in-memory access token is
    expired but a SIBLING has already refreshed and persisted a still-valid token,
    the coordinated refresh must re-read it under the lock and reuse it — NOT burn
    the single-use refresh token with another rotation."""
    monkeypatch.setattr(settings, "FANVUE_TOKENS_SOURCE", "secret")
    monkeypatch.setattr(settings, "GCP_PROJECT", "test-proj")
    # the persisted secret already holds a fresh token (far-future absolute expiry)
    _install_fake_secretmanager(monkeypatch, stored={
        "refresh_token": "r9", "access_token": "a9", "access_expires_at": 9_999_999_999})

    def boom_post(*a, **k):
        raise AssertionError("must not hit the token endpoint — sibling already refreshed")
    monkeypatch.setattr(fanvue_client.httpx, "post", boom_post)

    c = _fresh_client()
    monkeypatch.setattr(c, "_use_secret", lambda: True)
    c._tokens = {"access_token": "a_old", "refresh_token": "r_old"}
    c._access_expiry = 0.0                         # our copy looks expired

    assert c._access_token() == "a9"               # re-read sibling's token, no rotation


def test_retries_once_on_400_after_reloading_rotated_token(monkeypatch):
    """If our refresh token lost a rotation race (400 invalid_grant), the coordinated
    refresh must reload the latest persisted token ONCE and retry with it before
    giving up — this is exactly the lockout that bricked prod."""
    monkeypatch.setattr(settings, "FANVUE_TOKENS_SOURCE", "secret")

    c = _fresh_client()
    monkeypatch.setattr(c, "_use_secret", lambda: True)
    c._tokens = {"refresh_token": "r_stale"}
    c._access_expiry = 0.0

    loads = {"n": 0}

    def fake_load():
        loads["n"] += 1
        # 1st (re-read under lock) = still stale; 2nd (400 handler) = sibling rotated
        c._tokens = {"refresh_token": "r_stale" if loads["n"] == 1 else "r_new"}
        c._access_expiry = 0.0
    monkeypatch.setattr(c, "_load_tokens", fake_load)

    requested = []

    def fake_request(rt):
        requested.append(rt)
        if rt == "r_stale":
            resp = types.SimpleNamespace(status_code=400)
            raise fanvue_client.httpx.HTTPStatusError("400", request=None, response=resp)
        return {"access_token": "a2", "refresh_token": "r2", "expires_in": 3600}
    monkeypatch.setattr(c, "_token_request", fake_request)

    saved = []
    monkeypatch.setattr(c, "_save_tokens", lambda: saved.append(dict(c._tokens)))

    assert c._access_token() == "a2"
    assert requested == ["r_stale", "r_new"]       # retried with the reloaded token
    assert saved and saved[0]["refresh_token"] == "r2"
