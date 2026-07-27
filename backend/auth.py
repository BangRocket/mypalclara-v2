"""Clerk login for the TUI via OAuth 2.0 Authorization Code + PKCE.

Pattern: open the browser at the Clerk instance's /oauth/authorize, catch the
redirect on a one-shot 127.0.0.1 listener, exchange the code at /oauth/token,
and read the user identity from /oauth/userinfo. Public client + PKCE, so no
secret key is involved. See https://clerk.com/blog/adding-clerk-auth-to-your-cli

Config (.env): CLERK_ISSUER (Frontend API URL, e.g.
https://foo-12.clerk.accounts.dev) and CLERK_CLIENT_ID (OAuth application
client_id from the Clerk dashboard; redirect URI http://127.0.0.1/callback,
public client, PKCE required, scopes "profile email offline_access").

The token set is stored in the MyPalClara application database so login
persists across app restarts.
"""

import base64
import hashlib
import json
import os
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from dotenv import load_dotenv

from backend import session_store

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")

_LEGACY_SESSION_FILE = _ROOT / ".clerk-session.json"
REFRESH_SKEW_SECONDS = 30
LOGIN_TIMEOUT_SECONDS = 300
SCOPES = "profile email offline_access"


class AuthError(Exception):
    pass


def _config() -> tuple[str, str]:
    issuer = os.environ.get("CLERK_ISSUER", "").rstrip("/")
    client_id = os.environ.get("CLERK_CLIENT_ID", "")
    if not issuer or not client_id:
        raise AuthError(
            "CLERK_ISSUER and CLERK_CLIENT_ID must be set in .env "
            "(create an OAuth application in your Clerk dashboard first)"
        )
    return issuer, client_id


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


def load_session() -> dict | None:
    session = session_store.load("clerk", "current")
    if session is not None:
        return session

    # One-time migration from versions that persisted the token set on disk.
    try:
        session = json.loads(_LEGACY_SESSION_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    session_store.save("clerk", "current", session)
    _LEGACY_SESSION_FILE.unlink(missing_ok=True)
    return session


def _save_session(session: dict) -> None:
    session_store.save("clerk", "current", session)


def logout() -> None:
    """Clear the local session (best-effort revoke of the refresh token)."""
    session = load_session()
    if session and session.get("refresh_token"):
        try:
            issuer, client_id = _config()
            httpx.post(
                f"{issuer}/oauth/revoke",
                data={
                    "token": session["refresh_token"],
                    "client_id": client_id,
                },
                timeout=10,
            )
        except Exception:
            pass  # local logout still succeeds
    session_store.delete("clerk", "current")


def _expired(session: dict) -> bool:
    return time.time() >= session.get("expires_at", 0) - REFRESH_SKEW_SECONDS


def _refresh(session: dict) -> dict | None:
    issuer, client_id = _config()
    try:
        resp = httpx.post(
            f"{issuer}/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": session["refresh_token"],
                "client_id": client_id,
            },
            timeout=15,
        )
        resp.raise_for_status()
        tokens = resp.json()
    except Exception:
        return None
    session.update(_token_fields(tokens))
    _save_session(session)
    return session


def _token_fields(tokens: dict) -> dict:
    fields = {
        "access_token": tokens["access_token"],
        "expires_at": time.time() + int(tokens.get("expires_in", 3600)),
    }
    if tokens.get("refresh_token"):
        fields["refresh_token"] = tokens["refresh_token"]
    return fields


def ensure_session() -> dict | None:
    """A valid session from disk (refreshing if needed), or None."""
    session = load_session()
    if session is None:
        return None
    if _expired(session):
        if session.get("refresh_token"):
            return _refresh(session)
        return None
    return session


def current_user_id() -> str | None:
    """Clerk user id of the stored session, refreshing if needed."""
    session = ensure_session()
    return session.get("user_id") if session else None


class _CallbackHandler(BaseHTTPRequestHandler):
    auth_result: dict | None = None

    def do_GET(self) -> None:
        params = parse_qs(urlparse(self.path).query)
        _CallbackHandler.auth_result = {k: v[0] for k, v in params.items()}
        # Respond before the caller proceeds to the token exchange, so the
        # browser never hangs on a half-open connection.
        body = b"<h1>Signed in</h1><p>You can return to the terminal.</p>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # keep the terminal clean
        pass


def _await_callback(state: str) -> tuple[str, str, str]:
    """Run a one-shot listener; return (code, redirect_uri, pkce_verifier)."""
    _CallbackHandler.auth_result = None
    server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    server.timeout = LOGIN_TIMEOUT_SECONDS
    port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    try:
        verifier, challenge = _pkce_pair()
        issuer, client_id = _config()
        url = (
            f"{issuer}/oauth/authorize?response_type=code"
            f"&client_id={client_id}"
            f"&redirect_uri={redirect_uri}"
            f"&code_challenge={challenge}"
            f"&code_challenge_method=S256"
            f"&state={state}"
            f"&scope={SCOPES.replace(' ', '%20')}"
        )
        webbrowser.open(url)
        while _CallbackHandler.auth_result is None:
            server.handle_request()
            if _CallbackHandler.auth_result is None:
                raise AuthError("Timed out waiting for the browser login")
    finally:
        server.server_close()
    result = _CallbackHandler.auth_result or {}
    if result.get("state") != state:
        raise AuthError("Login failed: state mismatch (possible CSRF)")
    if "error" in result:
        raise AuthError(f"Login failed: {result.get('error_description') or result['error']}")
    if "code" not in result:
        raise AuthError("Login failed: no authorization code in callback")
    return result["code"], redirect_uri, verifier


def _exchange(code: str, redirect_uri: str, verifier: str) -> dict:
    issuer, client_id = _config()
    resp = httpx.post(
        f"{issuer}/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
        },
        timeout=15,
    )
    if resp.status_code >= 400:
        raise AuthError(f"Token exchange failed: {resp.status_code} {resp.text[:200]}")
    return resp.json()


def _userinfo(access_token: str) -> dict:
    issuer, _ = _config()
    resp = httpx.get(
        f"{issuer}/oauth/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    if resp.status_code >= 400:
        raise AuthError(f"userinfo failed: {resp.status_code} {resp.text[:200]}")
    return resp.json()


def login() -> dict:
    """Full browser login flow. Returns and stores the session dict."""
    state = secrets.token_urlsafe(16)
    code, redirect_uri, verifier = _await_callback(state)
    tokens = _exchange(code, redirect_uri, verifier)
    info = _userinfo(tokens["access_token"])
    session = {
        **_token_fields(tokens),
        "user_id": info["sub"],
        "email": info.get("email"),
        "name": info.get("name"),
    }
    _save_session(session)
    return session
