from __future__ import annotations

import base64
import hashlib
import io
import json
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.error import HTTPError

import pytest

import mail_dock.infrastructure.security.oauth2 as oauth2_module
from mail_dock.domain.errors import (
    AuthenticationError,
    ConfigError,
    CredentialStoreError,
    OperationCancelledError,
    TransientError,
)
from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.ports import AccessToken, OAuthTokenResponse
from mail_dock.infrastructure.security.oauth2 import OAuth2Client, OAuthAccessTokenProvider
from mail_dock.infrastructure.security.session_store import SessionCredentialStore


def test_google_authorization_uses_pkce_and_fixed_provider_endpoints() -> None:
    client = OAuth2Client()
    request = client.begin_authorization("google", "google-client")
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.authorization_url).query)
        pending = client._pending[request.request_id]
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(pending.code_verifier.encode("ascii")).digest())
            .rstrip(b"=")
            .decode("ascii")
        )

        assert request.redirect_uri.startswith("http://127.0.0.1:")
        assert query["response_type"] == ["code"]
        assert query["scope"] == ["https://mail.google.com/"]
        assert query["code_challenge_method"] == ["S256"]
        assert query["code_challenge"] == [challenge]
        assert query["access_type"] == ["offline"]
        assert query["prompt"] == ["consent"]
        assert urllib.parse.urlsplit(request.authorization_url).hostname == "accounts.google.com"
    finally:
        client._pending.pop(request.request_id).server.server_close()


def test_microsoft_authorization_uses_localhost_dynamic_redirect_and_allowed_tenant() -> None:
    client = OAuth2Client()
    request = client.begin_authorization("microsoft", "microsoft-client", tenant="organizations")
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.authorization_url).query)

        assert request.redirect_uri.startswith("http://localhost:")
        assert "/organizations/oauth2/v2.0/authorize" in request.authorization_url
        assert query["scope"] == ["https://outlook.office.com/IMAP.AccessAsUser.All offline_access"]
        assert query["response_mode"] == ["query"]
    finally:
        client._pending.pop(request.request_id).server.server_close()


@pytest.mark.parametrize(
    ("provider_name", "tenant"),
    [("google", None), ("microsoft", "organizations")],
)
def test_provider_endpoints_require_https_and_allowlisted_hosts(
    provider_name: str, tenant: str | None
) -> None:
    provider = oauth2_module._provider(provider_name, tenant)

    for endpoint in (provider.authorization_endpoint, provider.token_endpoint):
        parsed = urllib.parse.urlsplit(endpoint)
        assert parsed.scheme == "https"
        assert parsed.hostname in provider.allowed_hosts
        oauth2_module._validate_endpoint(endpoint, provider.allowed_hosts)
        with pytest.raises(ConfigError, match="approved HTTPS endpoint"):
            oauth2_module._validate_endpoint(
                endpoint.replace("https://", "http://", 1), provider.allowed_hosts
            )


def test_microsoft_localhost_redirect_accepts_loopback_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = OAuth2Client()
    request = client.begin_authorization("microsoft", "microsoft-client")
    pending = client._pending[request.request_id]
    tokens = OAuthTokenResponse("access-secret", datetime.now(UTC) + timedelta(hours=1))
    monkeypatch.setattr(client, "_request_tokens", lambda provider, values: tokens)

    with ThreadPoolExecutor(max_workers=1) as executor:
        completion = executor.submit(client.complete_authorization, request.request_id)
        callback_url = (
            f"{request.redirect_uri}?"
            f"{urllib.parse.urlencode({'state': pending.state, 'code': 'auth-code'})}"
        )
        with urllib.request.urlopen(callback_url, timeout=2) as response:
            assert response.status == 200
        assert completion.result(timeout=2) is tokens


def test_complete_authorization_validates_state_and_exchanges_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = OAuth2Client()
    request = client.begin_authorization("google", "client-id", "client-secret")
    pending = client._pending[request.request_id]
    state = pending.state
    captured: dict[str, str] = {}
    tokens = OAuthTokenResponse("access-secret", datetime.now(UTC) + timedelta(hours=1))
    monkeypatch.setattr(
        client,
        "_request_tokens",
        lambda provider, values: captured.update(values) or tokens,
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        completion = executor.submit(client.complete_authorization, request.request_id)
        callback_query = urllib.parse.urlencode({"state": state, "code": "auth-code"})
        callback_url = f"{request.redirect_uri}?{callback_query}"
        with urllib.request.urlopen(callback_url, timeout=2) as response:
            assert response.status == 200
        assert completion.result(timeout=2) is tokens

    assert captured["grant_type"] == "authorization_code"
    assert captured["code"] == "auth-code"
    assert captured["client_secret"] == "client-secret"
    assert captured["redirect_uri"] == request.redirect_uri
    assert "code_verifier" in captured


def test_complete_authorization_rejects_state_mismatch() -> None:
    client = OAuth2Client()
    request = client.begin_authorization("google", "client-id")
    with ThreadPoolExecutor(max_workers=1) as executor:
        completion = executor.submit(client.complete_authorization, request.request_id)
        callback_url = f"{request.redirect_uri}?state=wrong&code=auth-code"
        with urllib.request.urlopen(callback_url, timeout=2):
            pass
        with pytest.raises(AuthenticationError, match="state validation"):
            completion.result(timeout=2)
    assert request.request_id not in client._pending


def test_complete_authorization_observes_cancellation_and_closes_listener() -> None:
    client = OAuth2Client()
    request = client.begin_authorization("google", "client-id")
    cancel = CancelToken()
    cancel.cancel()

    with pytest.raises(OperationCancelledError):
        client.complete_authorization(request.request_id, cancel=cancel)
    assert request.request_id not in client._pending


def test_complete_authorization_timeout_closes_listener() -> None:
    client = OAuth2Client()
    request = client.begin_authorization("google", "client-id")

    with pytest.raises(TransientError, match="timed out"):
        client.complete_authorization(request.request_id, timeout=0.01)
    assert request.request_id not in client._pending


def test_refresh_token_rotation_is_saved_before_return(monkeypatch: pytest.MonkeyPatch) -> None:
    client = OAuth2Client()
    tokens = OAuthTokenResponse(
        "access-secret",
        datetime.now(UTC) + timedelta(hours=1),
        refresh_token="rotated-secret",
    )
    monkeypatch.setattr(client, "_request_tokens", lambda provider, values: tokens)
    saved: list[str] = []

    result = client.refresh_access_token(
        "google", "client-id", "old-secret", on_refresh_token_rotated=saved.append
    )

    assert result is tokens
    assert saved == ["rotated-secret"]
    assert "access-secret" not in repr(result)
    assert "rotated-secret" not in repr(result)


def test_refresh_posts_to_fixed_https_endpoint_and_parses_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse(io.BytesIO):
        status = 200

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *args: object) -> None:
            self.close()

    response = FakeResponse(
        json.dumps({"access_token": "access-secret", "expires_in": 3600}).encode()
    )
    captured: dict[str, object] = {}

    class FakeOpener:
        def open(self, request: urllib.request.Request, timeout: float) -> FakeResponse:
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            if not isinstance(request.data, bytes):
                raise AssertionError("token request body must be bytes")
            captured["form"] = urllib.parse.parse_qs(request.data.decode())
            return response

    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: FakeOpener())
    before = datetime.now(UTC)
    result = OAuth2Client().refresh_access_token(
        "google", "client-id", "refresh-secret", "client-secret"
    )

    assert captured["url"] == "https://oauth2.googleapis.com/token"
    assert captured["timeout"] == 30
    assert captured["form"] == {
        "grant_type": ["refresh_token"],
        "client_id": ["client-id"],
        "refresh_token": ["refresh-secret"],
        "scope": ["https://mail.google.com/"],
        "client_secret": ["client-secret"],
    }
    assert before + timedelta(seconds=3600) <= result.expires_at
    assert result.expires_at <= datetime.now(UTC) + timedelta(seconds=3601)


def test_microsoft_refresh_uses_allowlisted_tenant_endpoint_and_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse(io.BytesIO):
        status = 200

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *args: object) -> None:
            self.close()

    response = FakeResponse(
        json.dumps({"access_token": "access-token", "expires_in": 3600}).encode()
    )
    captured: dict[str, object] = {}

    class FakeOpener:
        def open(self, request: urllib.request.Request, timeout: float) -> FakeResponse:
            captured["url"] = request.full_url
            if not isinstance(request.data, bytes):
                raise AssertionError("token request body must be bytes")
            captured["form"] = urllib.parse.parse_qs(request.data.decode())
            return response

    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: FakeOpener())
    OAuth2Client().refresh_access_token(
        "microsoft", "client-id", "refresh-token", tenant="organizations"
    )

    assert captured["url"] == ("https://login.microsoftonline.com/organizations/oauth2/v2.0/token")
    assert captured["form"] == {
        "grant_type": ["refresh_token"],
        "client_id": ["client-id"],
        "refresh_token": ["refresh-token"],
        "scope": ["https://outlook.office.com/IMAP.AccessAsUser.All offline_access"],
    }


def test_local_http_stub_exercises_code_exchange_and_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, list[str]]] = []

    class TokenHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            form = urllib.parse.parse_qs(self.rfile.read(length).decode("ascii"))
            requests.append(form)
            payload = {
                "access_token": "exchanged-access-token",
                "expires_in": 3600,
                "token_type": "Bearer",
            }
            if form.get("grant_type") == ["authorization_code"]:
                payload["refresh_token"] = "initial-refresh-token"
            else:
                payload["refresh_token"] = "rotated-refresh-token"
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), TokenHandler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    token_endpoint = f"http://127.0.0.1:{server.server_port}/token"
    provider = oauth2_module._Provider(
        authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        token_endpoint=token_endpoint,
        allowed_hosts=frozenset({"accounts.google.com", "127.0.0.1"}),
        scopes=("https://mail.google.com/",),
        loopback_redirect_host="127.0.0.1",
    )

    def validate_test_endpoint(url: str, allowed_hosts: frozenset[str]) -> None:
        parsed = urllib.parse.urlsplit(url)
        assert parsed.hostname in allowed_hosts
        assert (parsed.scheme, parsed.hostname) in {
            ("https", "accounts.google.com"),
            ("http", "127.0.0.1"),
        }

    def use_local_provider(provider_name: str, tenant: str | None) -> oauth2_module._Provider:
        del provider_name, tenant
        return provider

    monkeypatch.setattr(oauth2_module, "_provider", use_local_provider)
    monkeypatch.setattr(oauth2_module, "_validate_endpoint", validate_test_endpoint)
    client = OAuth2Client()
    saved_refresh_tokens: list[str] = []

    try:
        authorization = client.begin_authorization("google", "client-id")
        pending = client._pending[authorization.request_id]
        code_verifier = pending.code_verifier
        with ThreadPoolExecutor(max_workers=1) as executor:
            completion = executor.submit(client.complete_authorization, authorization.request_id)
            callback = urllib.parse.urlencode({"state": pending.state, "code": "auth-code"})
            with urllib.request.urlopen(
                f"{authorization.redirect_uri}?{callback}", timeout=2
            ) as response:
                assert response.status == 200
            exchanged = completion.result(timeout=2)

        refreshed = client.refresh_access_token(
            "google",
            "client-id",
            exchanged.refresh_token or "",
            on_refresh_token_rotated=saved_refresh_tokens.append,
        )
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)

    assert exchanged.access_token == "exchanged-access-token"
    assert exchanged.refresh_token == "initial-refresh-token"
    assert refreshed.access_token == "exchanged-access-token"
    assert saved_refresh_tokens == ["rotated-refresh-token"]
    assert requests == [
        {
            "grant_type": ["authorization_code"],
            "client_id": ["client-id"],
            "code": ["auth-code"],
            "redirect_uri": [authorization.redirect_uri],
            "code_verifier": [code_verifier],
        },
        {
            "grant_type": ["refresh_token"],
            "client_id": ["client-id"],
            "refresh_token": ["initial-refresh-token"],
            "scope": ["https://mail.google.com/"],
        },
    ]


def test_refresh_rejects_unapproved_token_endpoint_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = oauth2_module._provider("google", None)
    unapproved_provider = oauth2_module._Provider(
        authorization_endpoint=provider.authorization_endpoint,
        token_endpoint="https://attacker.example/token",
        allowed_hosts=provider.allowed_hosts,
        scopes=provider.scopes,
        loopback_redirect_host=provider.loopback_redirect_host,
    )

    def reject_network(*handlers: object) -> object:
        del handlers
        raise AssertionError("unapproved endpoint must be rejected before opening a connection")

    monkeypatch.setattr(
        oauth2_module, "_provider", lambda provider_name, tenant: unapproved_provider
    )
    monkeypatch.setattr(urllib.request, "build_opener", reject_network)

    with pytest.raises(ConfigError, match="approved HTTPS endpoint"):
        OAuth2Client().refresh_access_token("google", "client-id", "refresh-secret")


def test_invalid_grant_is_classified_as_authentication_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class FakeOpener:
        def open(self, request: urllib.request.Request, timeout: float) -> object:
            del timeout
            raise HTTPError(
                request.full_url,
                400,
                "Bad Request",
                Message(),
                io.BytesIO(
                    b'{"error":"invalid_grant","error_description":"client-secret-sentinel '
                    b'refresh-token-sentinel access-token-sentinel"}'
                ),
            )

    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: FakeOpener())

    with pytest.raises(AuthenticationError) as raised:
        OAuth2Client().refresh_access_token("google", "client-id", "refresh-secret")
    assert "invalid_grant" not in str(raised.value)
    assert all(
        secret not in str(raised.value) and secret not in caplog.text
        for secret in (
            "client-secret-sentinel",
            "refresh-token-sentinel",
            "access-token-sentinel",
            "refresh-secret",
        )
    )


def test_non_json_token_endpoint_server_error_is_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeOpener:
        def open(self, request: urllib.request.Request, timeout: float) -> object:
            del timeout
            raise HTTPError(
                request.full_url,
                503,
                "Service Unavailable",
                Message(),
                io.BytesIO(b"upstream unavailable"),
            )

    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: FakeOpener())

    with pytest.raises(TransientError):
        OAuth2Client().refresh_access_token("google", "client-id", "refresh-secret")


def test_refresh_token_rotation_storage_failure_is_not_suppressed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = OAuth2Client()
    tokens = OAuthTokenResponse(
        "access-secret",
        datetime.now(UTC) + timedelta(hours=1),
        refresh_token="rotated-secret",
    )
    monkeypatch.setattr(client, "_request_tokens", lambda provider, values: tokens)

    def fail_to_save(value: str) -> None:
        del value
        raise OSError("keyring unavailable")

    with pytest.raises(CredentialStoreError, match="rotated OAuth refresh token"):
        client.refresh_access_token(
            "google", "client-id", "old-secret", on_refresh_token_rotated=fail_to_save
        )


def test_access_token_provider_refreshes_with_margin_and_caches_in_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionCredentialStore()
    store.set_secret("account", "refresh_token", "refresh-secret")
    store.set_secret("account", "client_secret", "client-secret")
    client = OAuth2Client()
    calls: list[tuple[str, str, str, str | None]] = []

    def refresh(
        provider: str,
        client_id: str,
        refresh_token: str,
        client_secret: str | None = None,
        *,
        tenant: str | None = None,
        on_refresh_token_rotated: object = None,
    ) -> OAuthTokenResponse:
        del tenant
        calls.append((provider, client_id, refresh_token, client_secret))
        callback = on_refresh_token_rotated
        if callable(callback):
            callback("rotated-refresh-secret")
        return OAuthTokenResponse(
            f"access-{len(calls)}",
            datetime.now(UTC) + timedelta(hours=1),
        )

    monkeypatch.setattr(client, "refresh_access_token", refresh)
    provider = OAuthAccessTokenProvider(
        client,
        store,
        account_id="account",
        provider="google",
        client_id="client-id",
    )

    first = provider.get_access_token("account")
    second = provider.get_access_token("account")
    provider._access_token = AccessToken("near-expiry", datetime.now(UTC) + timedelta(seconds=60))
    third = provider.get_access_token("account")

    assert first.value == "access-1"
    assert second is first
    assert third.value == "access-2"
    assert len(calls) == 2
    assert calls == [
        ("google", "client-id", "refresh-secret", "client-secret"),
        ("google", "client-id", "rotated-refresh-secret", "client-secret"),
    ]
    assert store.get_secret("account", "refresh_token") == "rotated-refresh-secret"
    assert "access-1" not in repr(first)


def test_access_token_provider_keeps_refresh_token_when_response_omits_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionCredentialStore()
    store.set_secret("account", "refresh_token", "existing-refresh-token")
    client = OAuth2Client()

    def refresh_without_rotation(
        provider: str,
        client_id: str,
        refresh_token: str,
        client_secret: str | None = None,
        *,
        tenant: str | None = None,
        on_refresh_token_rotated: object = None,
    ) -> OAuthTokenResponse:
        del provider, client_id, refresh_token, client_secret, tenant, on_refresh_token_rotated
        return OAuthTokenResponse("access-token", datetime.now(UTC) + timedelta(hours=1))

    monkeypatch.setattr(client, "refresh_access_token", refresh_without_rotation)
    provider = OAuthAccessTokenProvider(
        client,
        store,
        account_id="account",
        provider="google",
        client_id="client-id",
    )

    provider.get_access_token("account")

    assert store.get_secret("account", "refresh_token") == "existing-refresh-token"


def test_access_token_provider_requires_a_refresh_token() -> None:
    provider = OAuthAccessTokenProvider(
        OAuth2Client(),
        SessionCredentialStore(),
        account_id="account",
        provider="google",
        client_id="client-id",
    )

    with pytest.raises(AuthenticationError, match="reauthorization"):
        provider.get_access_token("account")


def test_rotated_refresh_token_requires_a_persistence_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tokens = OAuthTokenResponse(
        "access-secret",
        datetime.now(UTC) + timedelta(hours=1),
        refresh_token="rotated-secret",
    )
    client = OAuth2Client()
    monkeypatch.setattr(client, "_request_tokens", lambda provider, values: tokens)

    with pytest.raises(CredentialStoreError, match="persistence callback"):
        client.refresh_access_token("google", "client-id", "old-secret")


def test_client_secret_and_refresh_tokens_are_not_in_token_repr() -> None:
    response = OAuthTokenResponse(
        "access-secret", datetime.now(UTC), refresh_token="refresh-secret"
    )

    assert "access-secret" not in repr(response)
    assert "refresh-secret" not in repr(response)


def test_non_allowlisted_provider_and_tenant_are_rejected() -> None:
    client = OAuth2Client()
    with pytest.raises(ConfigError, match="unsupported OAuth provider"):
        client.begin_authorization("https://attacker.example", "client-id")
    with pytest.raises(ConfigError, match="Microsoft tenant"):
        client.begin_authorization("microsoft", "client-id", tenant="attacker.example")
