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
