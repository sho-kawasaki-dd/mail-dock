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
from urllib.error import HTTPError

import pytest

from mail_dock.domain.errors import (
    AuthenticationError,
    ConfigError,
    CredentialStoreError,
    OperationCancelledError,
    TransientError,
)
from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.ports import OAuthTokenResponse
from mail_dock.infrastructure.security.oauth2 import OAuth2Client


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


def test_invalid_grant_is_classified_as_authentication_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeOpener:
        def open(self, request: urllib.request.Request, timeout: float) -> object:
            del timeout
            raise HTTPError(
                request.full_url,
                400,
                "Bad Request",
                Message(),
                io.BytesIO(b'{"error":"invalid_grant","error_description":"private detail"}'),
            )

    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: FakeOpener())

    with pytest.raises(AuthenticationError) as raised:
        OAuth2Client().refresh_access_token("google", "client-id", "refresh-secret")
    assert "private detail" not in str(raised.value)


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
