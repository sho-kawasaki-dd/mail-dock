from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from mail_dock.domain.errors import AuthenticationError
from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.ports import (
    BaseOAuthClient,
    OAuthAuthorizationRequest,
    OAuthTokenResponse,
)
from mail_dock.infrastructure.security.session_store import SessionCredentialStore
from mail_dock.usecases.oauth_authorize import begin_authorization, complete_authorization


class FakeOAuthClient(BaseOAuthClient):
    def __init__(self, response: OAuthTokenResponse) -> None:
        self.response = response
        self.authorization_arguments: tuple[object, ...] | None = None
        self.completion_arguments: tuple[object, ...] | None = None

    def begin_authorization(
        self,
        provider: str,
        client_id: str,
        client_secret: str | None = None,
        *,
        tenant: str | None = None,
    ) -> OAuthAuthorizationRequest:
        self.authorization_arguments = (provider, client_id, client_secret, tenant)
        return OAuthAuthorizationRequest(
            "request",
            "https://example.test/authorize",
            "http://127.0.0.1:1/",
        )

    def complete_authorization(
        self,
        request_id: str,
        *,
        timeout: float = 120.0,
        cancel: CancelToken | None = None,
    ) -> OAuthTokenResponse:
        self.completion_arguments = (request_id, timeout, cancel)
        return self.response

    def refresh_access_token(
        self,
        provider: str,
        client_id: str,
        refresh_token: str,
        client_secret: str | None = None,
        *,
        tenant: str | None = None,
        on_refresh_token_rotated: Callable[[str], None] | None = None,
    ) -> OAuthTokenResponse:
        del provider, client_id, refresh_token, client_secret, tenant, on_refresh_token_rotated
        raise AssertionError("authorization use case must not refresh tokens")


def test_authorization_stores_client_secret_and_only_persists_refresh_token() -> None:
    store = SessionCredentialStore()
    client = FakeOAuthClient(
        OAuthTokenResponse(
            "access-secret",
            datetime.now(UTC) + timedelta(hours=1),
            refresh_token="refresh-secret",
        )
    )
    request = begin_authorization(
        client,
        store,
        account_id="account",
        provider="google",
        client_id="client-id",
        client_secret="client-secret",
    )
    cancel = CancelToken()

    complete_authorization(
        client,
        store,
        account_id="account",
        request_id=request.request_id,
        timeout=90,
        cancel=cancel,
    )

    assert client.authorization_arguments == ("google", "client-id", "client-secret", None)
    assert client.completion_arguments == ("request", 90, cancel)
    assert store.get_secret("account", "client_secret") == "client-secret"
    assert store.get_secret("account", "refresh_token") == "refresh-secret"
    assert store.get_secret("account", "access_token") is None


def test_authorization_requires_a_refresh_token_for_persistent_access() -> None:
    store = SessionCredentialStore()
    client = FakeOAuthClient(
        OAuthTokenResponse("access-secret", datetime.now(UTC) + timedelta(hours=1))
    )
    request = begin_authorization(
        client,
        store,
        account_id="account",
        provider="google",
        client_id="client-id",
    )

    with pytest.raises(AuthenticationError, match="did not issue a refresh token"):
        complete_authorization(
            client,
            store,
            account_id="account",
            request_id=request.request_id,
        )
    assert store.get_secret("account", "refresh_token") is None
