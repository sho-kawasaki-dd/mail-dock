#!/usr/bin/env python3
"""Throwaway PoC for Phase 5 Group G (docs/実装計画書_Phase5_マルチプロトコル対応.md).

Verifies, against the real Google OAuth2 and Gmail IMAP endpoints, that:
  - G-2: Authorization Code + PKCE (S256) + loopback redirect succeeds using
    only the Python standard library (no third-party OAuth packages; D-15).
  - G-3: the resulting access/refresh token authenticates over IMAP XOAUTH2
    and `UID FETCH` returns X-GM-MSGID / X-GM-THRID / X-GM-LABELS.

This script is NOT part of the application (src/mail_dock/). Per D-28, OAuth2
consent flows are GUI-only in the product; this is a manual, one-off
verification tool only. It must never be imported by application code.

Usage:
    export GMAIL_CLIENT_ID="...apps.googleusercontent.com"
    export GMAIL_CLIENT_SECRET="..."          # from the Desktop App OAuth client (G-1)
    export GMAIL_USER_EMAIL="you@gmail.com"   # the test user registered in G-1
    python tools/gmail_oauth_poc/gmail_poc.py

Security notes:
  - client_secret / access_token / refresh_token are read from the
    environment and are never printed, logged, or written to disk by this
    script. Only lengths/booleans are printed for sanity checking.
  - The loopback callback server binds 127.0.0.1 only, serves exactly one
    request, and always shuts down afterwards (success, mismatch, or
    timeout) so it can never linger as an open listening socket.
  - Do not commit any captured code/tokens. If a token is ever printed to a
    terminal or log by accident, revoke it in
    https://myaccount.google.com/permissions before continuing.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import imaplib
import json
import os
import secrets
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_IMAP_HOST = "imap.gmail.com"
_IMAP_PORT = 993
_SCOPE = "https://mail.google.com/"
_CALLBACK_TIMEOUT_SECONDS = 120.0


class _CallbackResult:
    def __init__(self) -> None:
        self.code: str | None = None
        self.state: str | None = None
        self.error: str | None = None


def _make_callback_handler(result: _CallbackResult) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (stdlib method name)
            query = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(query)
            result.code = params.get("code", [None])[0]
            result.state = params.get("state", [None])[0]
            result.error = params.get("error", [None])[0]

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h1>mail-dock Gmail OAuth PoC</h1>"
                b"<p>Authorization step complete. You can close this tab "
                b"and return to the terminal.</p></body></html>"
            )

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass  # suppress default request logging (no secrets in it, but keep output clean)

    return Handler


def _generate_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _run_authorization_step(client_id: str) -> tuple[str, str]:
    """Run G-2: obtain an authorization code via PKCE + loopback redirect.

    Returns (auth_code, redirect_uri). Raises SystemExit on any mismatch,
    provider-reported error, or timeout.
    """

    code_verifier, code_challenge = _generate_pkce_pair()
    state = secrets.token_urlsafe(32)
    result = _CallbackResult()

    server = http.server.HTTPServer(("127.0.0.1", 0), _make_callback_handler(result))
    server.timeout = _CALLBACK_TIMEOUT_SECONDS
    port = server.server_port
    redirect_uri = f"http://127.0.0.1:{port}/"

    auth_params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": _SCOPE,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    }
    auth_url = f"{_AUTH_ENDPOINT}?{urllib.parse.urlencode(auth_params)}"
    print(f"[G-2] Redirect URI for this run: {redirect_uri}")
    print("[G-2] Opening the default browser for Google sign-in/consent...")
    if not webbrowser.open(auth_url):
        print(f"[G-2] Could not launch a browser automatically. Open this URL manually:\n{auth_url}")

    def _stop_after_timeout() -> None:
        server.server_close()

    watchdog = threading.Timer(_CALLBACK_TIMEOUT_SECONDS, _stop_after_timeout)
    watchdog.daemon = True
    watchdog.start()
    try:
        server.handle_request()  # blocks for at most `server.timeout` seconds
    finally:
        watchdog.cancel()
        server.server_close()

    if result.error is not None:
        raise SystemExit(f"[G-2] Google returned an error: {result.error}")
    if result.code is None:
        raise SystemExit("[G-2] Timed out waiting for the OAuth callback (no code received).")
    if result.state != state:
        raise SystemExit("[G-2] state mismatch on callback — aborting (possible CSRF).")

    print("[G-2] Authorization code received and state verified.")
    return result.code, redirect_uri, code_verifier  # type: ignore[return-value]


def _exchange_code_for_tokens(
    *, client_id: str, client_secret: str, code: str, code_verifier: str, redirect_uri: str
) -> dict[str, object]:
    """Run the token-exchange half of G-2/G-3."""

    body = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "code_verifier": code_verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
    ).encode("ascii")
    request = urllib.request.Request(_TOKEN_ENDPOINT, data=body, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30.0) as response:  # noqa: S310 (fixed HTTPS endpoint)
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise SystemExit(f"[G-2] token exchange failed ({error.code}): {detail}") from error


class _Xoauth2Callback:
    """Stateful SASL callback: send the initial response once, then '' on
    any subsequent continuation challenge (Phase 5 review finding #1)."""

    def __init__(self, email: str, access_token: str) -> None:
        self._sasl_string = f"user={email}\x01auth=Bearer {access_token}\x01\x01".encode("utf-8")
        self._called = False

    def __call__(self, _challenge: bytes) -> bytes:
        if not self._called:
            self._called = True
            return self._sasl_string
        return b""


def _verify_imap_xoauth2(email: str, access_token: str) -> None:
    """Run G-3: XOAUTH2 login, LIST, and UID FETCH of X-GM-* attributes."""

    imap = imaplib.IMAP4_SSL(_IMAP_HOST, _IMAP_PORT, timeout=30.0)
    try:
        status, data = imap.authenticate("XOAUTH2", _Xoauth2Callback(email, access_token))
        print(f"[G-3] XOAUTH2 authenticate: {status}")
        if status != "OK":
            raise SystemExit(f"[G-3] XOAUTH2 authentication failed: {data!r}")

        status, folders = imap.list('""', "*")
        print(f"[G-3] LIST returned {len(folders or [])} folder(s):")
        for raw in folders or []:
            text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
            print(f"       {text}")

        imap.select("INBOX", readonly=True)
        status, search_data = imap.uid("SEARCH", None, "ALL")
        uids = search_data[0].split() if search_data and search_data[0] else []
        if not uids:
            print("[G-3] INBOX has no messages to FETCH; X-GM-* could not be sampled.")
            return

        target_uid = uids[-1].decode("ascii")
        status, fetch_data = imap.uid(
            "FETCH", target_uid, "(UID FLAGS X-GM-MSGID X-GM-THRID X-GM-LABELS)"
        )
        print(f"[G-3] UID FETCH {target_uid} response:")
        for item in fetch_data or []:
            if isinstance(item, tuple):
                text = item[0].decode("utf-8", errors="replace")
            elif isinstance(item, bytes):
                text = item.decode("utf-8", errors="replace")
            else:
                text = str(item)
            print(f"       {text}")
            for marker in ("X-GM-MSGID", "X-GM-THRID", "X-GM-LABELS"):
                if marker not in text:
                    print(f"       WARNING: {marker} not found in this response line")
    finally:
        with __import__("contextlib").suppress(Exception):
            imap.logout()


def main() -> int:
    client_id = os.environ.get("GMAIL_CLIENT_ID")
    client_secret = os.environ.get("GMAIL_CLIENT_SECRET")
    user_email = os.environ.get("GMAIL_USER_EMAIL")
    missing = [
        name
        for name, value in (
            ("GMAIL_CLIENT_ID", client_id),
            ("GMAIL_CLIENT_SECRET", client_secret),
            ("GMAIL_USER_EMAIL", user_email),
        )
        if not value
    ]
    if missing:
        print(f"Missing required environment variable(s): {', '.join(missing)}", file=sys.stderr)
        return 2

    assert client_id is not None
    assert client_secret is not None
    assert user_email is not None

    auth_code, redirect_uri, code_verifier = _run_authorization_step(client_id)
    tokens = _exchange_code_for_tokens(
        client_id=client_id,
        client_secret=client_secret,
        code=auth_code,
        code_verifier=code_verifier,
        redirect_uri=redirect_uri,
    )

    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    if not isinstance(access_token, str):
        raise SystemExit("[G-2] Token response did not include an access_token.")
    print(
        "[G-2] Token exchange succeeded "
        f"(access_token length={len(access_token)}, refresh_token present={refresh_token is not None})."
    )
    if refresh_token is None:
        print(
            "[G-2] WARNING: no refresh_token returned. Revoke prior consent at "
            "https://myaccount.google.com/permissions and re-run so `prompt=consent` "
            "forces a fresh one (needed for G-3/token-refresh verification later)."
        )

    _verify_imap_xoauth2(user_email, access_token)
    print("[G-2/G-3] PoC completed. Record the outcome in Group G of the implementation plan.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
