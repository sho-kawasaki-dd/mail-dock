"""Use cases for starting and completing OAuth account authorization."""

from __future__ import annotations

from mail_dock.domain.errors import AuthenticationError
from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.ports import (
    BaseCredentialStore,
    BaseOAuthClient,
    OAuthAuthorizationRequest,
)


def begin_authorization(
    oauth_client: BaseOAuthClient,
    credential_store: BaseCredentialStore,
    *,
    account_id: str,
    provider: str,
    client_id: str,
    client_secret: str | None = None,
    tenant: str | None = None,
) -> OAuthAuthorizationRequest:
    """Start a new authorization, including reauthorization after revocation."""

    if client_secret is not None:
        credential_store.set_secret(account_id, "client_secret", client_secret)
    return oauth_client.begin_authorization(
        provider,
        client_id,
        client_secret,
        tenant=tenant,
    )


def complete_authorization(
    oauth_client: BaseOAuthClient,
    credential_store: BaseCredentialStore,
    *,
    account_id: str,
    request_id: str,
    timeout: float = 120.0,
    cancel: CancelToken | None = None,
) -> None:
    """Wait for the loopback callback and persist only the refresh token."""

    tokens = oauth_client.complete_authorization(
        request_id,
        timeout=timeout,
        cancel=cancel,
    )
    if tokens.refresh_token is None:
        raise AuthenticationError("OAuth provider did not issue a refresh token")
    credential_store.set_secret(account_id, "refresh_token", tokens.refresh_token)
