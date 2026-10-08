#!/usr/bin/env python3
"""
One-time Fanvue OAuth bootstrap.

Run this ONCE on your laptop. It:
  1. opens your browser to Fanvue's "Allow" screen,
  2. captures the authorization code on a local callback,
  3. exchanges it (PKCE + HTTP Basic auth) for tokens,
  4. saves the refresh token to .fanvue_tokens.json (gitignored).

After this, the GCP agent uses the refresh token to renew access forever —
no browser needed again.

Usage:
    python3 fanvue_auth.py
Requires in .env.local: OAUTH_CLIENT_ID, OAUTH_CLIENT_SECRET
"""
import base64, hashlib, http.server, json, os, secrets, threading, urllib.parse, urllib.request, webbrowser, sys

# ---- config ----------------------------------------------------------------
AUTH_URL  = "https://auth.fanvue.com/oauth2/auth"
TOKEN_URL = "https://auth.fanvue.com/oauth2/token"
PORT      = int(os.environ.get("OAUTH_CALLBACK_PORT", "8080"))
REDIRECT  = os.environ.get("OAUTH_REDIRECT_URI", f"http://localhost:{PORT}/callback")
SCOPES    = os.environ.get("OAUTH_SCOPES", "openid offline_access read:self read:chat write:chat")
TOKENS_FILE = "secrets/.fanvue_tokens.json"


def load_env(path="secrets/.env"):
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()
CLIENT_ID = os.environ["OAUTH_CLIENT_ID"]
CLIENT_SECRET = os.environ["OAUTH_CLIENT_SECRET"]

# ---- PKCE ------------------------------------------------------------------
verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
challenge = base64.urlsafe_b64encode(
    hashlib.sha256(verifier.encode()).digest()
).rstrip(b"=").decode()
state = secrets.token_urlsafe(16)

auth_params = urllib.parse.urlencode({
    "response_type": "code",
    "client_id": CLIENT_ID,
    "redirect_uri": REDIRECT,
    "scope": SCOPES,
    "state": state,
    "code_challenge": challenge,
    "code_challenge_method": "S256",
})
authorize_link = f"{AUTH_URL}?{auth_params}"

_result = {}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != urllib.parse.urlparse(REDIRECT).path:
            self.send_response(404); self.end_headers(); return
        q = urllib.parse.parse_qs(parsed.query)
        _result.update({k: v[0] for k, v in q.items()})
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        ok = "code" in _result and _result.get("state") == state
        msg = "✅ Authorized — you can close this tab and return to the terminal." if ok \
              else f"❌ Error: {_result}"
        self.wfile.write(f"<html><body style='font-family:sans-serif'><h2>{msg}</h2></body></html>".encode())

    def log_message(self, *a):  # silence
        pass


def exchange(code):
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT,
        "code_verifier": verifier,
    }).encode()
    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    req = urllib.request.Request(TOKEN_URL, data=data, headers={
        "Authorization": f"Basic {basic}",
        "Content-Type": "application/x-www-form-urlencoded",
    })
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def main():
    server = http.server.HTTPServer(("localhost", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    print(f"\nRedirect URI (must be registered in Fanvue app): {REDIRECT}")
    print(f"Scopes: {SCOPES}\n")
    print("Opening your browser to authorize… if it doesn't open, paste this:\n")
    print(authorize_link + "\n")
    webbrowser.open(authorize_link)

    print("Waiting for you to click Allow…")
    while "code" not in _result and "error" not in _result:
        threading.Event().wait(0.3)
    server.shutdown()

    if "error" in _result:
        print("\n❌ Authorization failed:", _result); sys.exit(1)
    if _result.get("state") != state:
        print("\n❌ State mismatch — aborting for safety."); sys.exit(1)

    print("\nExchanging code for tokens…")
    try:
        tok = exchange(_result["code"])
    except urllib.error.HTTPError as e:
        print("❌ Token exchange failed:", e.read().decode()); sys.exit(1)

    with open(TOKENS_FILE, "w") as f:
        json.dump(tok, f, indent=2)
    print(f"\n✅ Saved tokens to {TOKENS_FILE}")
    print("   access_token:", (tok.get("access_token", "")[:18] + "…") if tok.get("access_token") else "MISSING")
    print("   refresh_token:", "present ✅" if tok.get("refresh_token") else "MISSING ❌ (did you include offline_access?)")
    print("   expires_in:", tok.get("expires_in"), "seconds")


if __name__ == "__main__":
    main()
