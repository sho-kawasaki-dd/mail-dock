"""Standard-library OAuth 2.0 authorization-code and refresh support."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Lock
from typing import cast
from uuid import UUID

from mail_dock.domain.errors import (
    AuthenticationError,
    ConfigError,
    CredentialStoreError,
    OperationCancelledError,
    PermanentError,
    TransientError,
)
from mail_dock.domain.fetcher import CancelToken
from mail_dock.domain.ports import (
    AccessToken,
    BaseAccessTokenProvider,
    BaseCredentialStore,
    BaseOAuthClient,
    OAuthAuthorizationRequest,
    OAuthTokenResponse,
)

_CALLBACK_POLL_SECONDS = 0.2
_TOKEN_REQUEST_TIMEOUT_SECONDS = 30
_MAX_TOKEN_RESPONSE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class _Provider:
    authorization_endpoint: str
    token_endpoint: str
    allowed_hosts: frozenset[str]
    scopes: tuple[str, ...]
    loopback_redirect_host: str
    microsoft: bool = False


@dataclass(frozen=True, repr=False)
class _PendingAuthorization:
    provider: _Provider
    client_id: str
    client_secret: str | None = field(repr=False)
    redirect_uri: str
    state: str = field(repr=False)
    code_verifier: str = field(repr=False)
    server: _CallbackServer = field(repr=False)


class _CallbackServer(HTTPServer):
    allow_reuse_address = False
    request_queue_size = 1

    callback_result: tuple[str | None, str | None] | None

    def __init__(
        self, server_address: tuple[str, int], handler: type[BaseHTTPRequestHandler]
    ) -> None:
        self.callback_result = None
        super().__init__(server_address, handler, bind_and_activate=True)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


def _callback_handler(expected_state: str) -> type[BaseHTTPRequestHandler]:
    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            server = cast(_CallbackServer, self.server)
            parsed = urllib.parse.urlsplit(self.path)
            values = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            received_state = values.get("state", [])
            code_values = values.get("code", [])
            error_values = values.get("error", [])
            result: tuple[str | None, str | None]

            if parsed.path != "/" or len(received_state) != 1:
                result = (None, "invalid_callback")
            elif not hmac.compare_digest(received_state[0], expected_state):
                result = (None, "invalid_state")
            elif len(error_values) == 1:
                result = (None, "authorization_denied")
            elif len(code_values) != 1 or not code_values[0]:
                result = (None, "missing_code")
            else:
                result = (code_values[0], None)

            server.callback_result = result
            body = (
                b"Authorization completed. You may close this window."
                if result[1] is None
                else b"Authorization could not be completed. Return to mail-dock."
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    return CallbackHandler


class OAuth2Client(BaseOAuthClient):
    """OAuth client with fixed provider endpoints and a one-shot loopback listener."""

    def __init__(self) -> None:
        self._pending: dict[str, _PendingAuthorization] = {}
        self._pending_lock = Lock()

    def begin_authorization(
        self,
        provider: str,
        client_id: str,
        client_secret: str | None = None,
        *,
        tenant: str | None = None,
    ) -> OAuthAuthorizationRequest:
        if not client_id.strip():
            raise ConfigError("OAuth client ID is required")
        definition = _provider(provider, tenant)
        _validate_endpoint(definition.authorization_endpoint, definition.allowed_hosts)

        state = secrets.token_urlsafe(32)
        try:
            server = _CallbackServer(
                (definition.loopback_redirect_host, 0), _callback_handler(state)
            )
        except OSError as error:
            raise ConfigError("could not start the OAuth loopback callback listener") from error

        port = server.server_address[1]
        redirect_host = definition.loopback_redirect_host
        redirect_uri = _redirect_uri(redirect_host, port)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        code_challenge = challenge.rstrip(b"=").decode("ascii")
        request_id = secrets.token_urlsafe(24)

        parameters = {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": " ".join(definition.scopes),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        if definition.microsoft:
            parameters["response_mode"] = "query"
        else:
            parameters["access_type"] = "offline"
            parameters["prompt"] = "consent"
        authorization_url = (
            f"{definition.authorization_endpoint}?{urllib.parse.urlencode(parameters)}"
        )
        with self._pending_lock:
            self._pending[request_id] = _PendingAuthorization(
                provider=definition,
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=redirect_uri,
                state=state,
                code_verifier=verifier,
                server=server,
            )
        return OAuthAuthorizationRequest(request_id, authorization_url, redirect_uri)

    def complete_authorization(
        self,
        request_id: str,
        *,
        timeout: float = 120.0,
        cancel: CancelToken | None = None,
    ) -> OAuthTokenResponse:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive")
        with self._pending_lock:
            pending = self._pending.pop(request_id, None)
        if pending is None:
            raise ConfigError("OAuth authorization request is missing or already completed")

        try:
            code = self._wait_for_code(pending, timeout=timeout, cancel=cancel)
            values = {
                "grant_type": "authorization_code",
                "client_id": pending.client_id,
                "code": code,
                "redirect_uri": pending.redirect_uri,
                "code_verifier": pending.code_verifier,
            }
            if pending.client_secret:
                values["client_secret"] = pending.client_secret
            return self._request_tokens(pending.provider, values)
        finally:
            pending.server.server_close()

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
        if not client_id.strip() or not refresh_token:
            raise ConfigError("OAuth client ID and refresh token are required")
        definition = _provider(provider, tenant)
        values = {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
            "scope": " ".join(definition.scopes),
        }
        if client_secret:
            values["client_secret"] = client_secret
        tokens = self._request_tokens(definition, values)
        if tokens.refresh_token is not None:
            if on_refresh_token_rotated is None:
                raise CredentialStoreError(
                    "cannot accept a rotated OAuth refresh token without a persistence callback"
                )
            try:
                on_refresh_token_rotated(tokens.refresh_token)
            except Exception as error:
                raise CredentialStoreError(
                    "could not save the rotated OAuth refresh token"
                ) from error
        return tokens

    @staticmethod
    def _wait_for_code(
        pending: _PendingAuthorization,
        *,
        timeout: float,
        cancel: CancelToken | None,
    ) -> str:
        deadline = time.monotonic() + timeout
        server = pending.server
        server.timeout = _CALLBACK_POLL_SECONDS
        while server.callback_result is None:
            if cancel is not None and cancel.is_cancelled:
                raise OperationCancelledError("OAuth authorization cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransientError("OAuth authorization timed out")
            server.timeout = min(_CALLBACK_POLL_SECONDS, remaining)
            try:
                server.handle_request()
            except OSError as error:
                raise TransientError("OAuth callback listener failed") from error

        code, failure = server.callback_result
        if failure == "authorization_denied":
            raise AuthenticationError("OAuth authorization was denied")
        if failure == "invalid_state":
            raise AuthenticationError("OAuth state validation failed")
        if failure is not None or code is None:
            raise AuthenticationError("OAuth callback was invalid")
        return code

    @classmethod
    def _request_tokens(cls, provider: _Provider, values: Mapping[str, str]) -> OAuthTokenResponse:
        _validate_endpoint(provider.token_endpoint, provider.allowed_hosts)
        request = urllib.request.Request(
            provider.token_endpoint,
            data=urllib.parse.urlencode(values).encode("ascii"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        opener = urllib.request.build_opener(_NoRedirectHandler())
        try:
            with opener.open(request, timeout=_TOKEN_REQUEST_TIMEOUT_SECONDS) as response:
                status = response.status
                body = response.read(_MAX_TOKEN_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            status = error.code
            body = error.read(_MAX_TOKEN_RESPONSE_BYTES + 1)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise TransientError("OAuth token endpoint is temporarily unavailable") from error

        if len(body) > _MAX_TOKEN_RESPONSE_BYTES:
            raise PermanentError("OAuth token endpoint returned an oversized response")
        if status == 429 or status >= 500:
            raise TransientError("OAuth token endpoint is temporarily unavailable")
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PermanentError("OAuth token endpoint returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise PermanentError("OAuth token endpoint returned an invalid response")
        oauth_error = payload.get("error")
        if oauth_error is not None and not isinstance(oauth_error, str):
            raise PermanentError("OAuth token endpoint returned an invalid error response")
        if status < 200 or status >= 300 or oauth_error is not None:
            if oauth_error in {"invalid_grant", "invalid_client", "unauthorized_client"}:
                raise AuthenticationError(
                    "OAuth credentials are invalid or require reauthorization"
                )
            raise PermanentError("OAuth token request failed")

        access_token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        refresh_token = payload.get("refresh_token")
        token_type = payload.get("token_type", "Bearer")
        if (
            not isinstance(access_token, str)
            or not access_token
            or isinstance(expires_in, bool)
            or not isinstance(expires_in, int)
            or expires_in <= 0
            or (
                refresh_token is not None
                and (not isinstance(refresh_token, str) or not refresh_token)
            )
            or not isinstance(token_type, str)
            or not token_type
        ):
            raise PermanentError("OAuth token endpoint returned incomplete token data")
        try:
            expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
        except OverflowError as error:
            raise PermanentError("OAuth token expiry is invalid") from error
        return OAuthTokenResponse(
            access_token=access_token,
            expires_at=expires_at,
            refresh_token=refresh_token,
            token_type=token_type,
        )


class OAuthAccessTokenProvider(BaseAccessTokenProvider):
    """Refresh and cache one account's access token in process memory only."""

    def __init__(
        self,
        oauth_client: BaseOAuthClient,
        credential_store: BaseCredentialStore,
        *,
        account_id: str,
        provider: str,
        client_id: str,
        tenant: str | None = None,
    ) -> None:
        if not account_id or not provider or not client_id:
            raise ConfigError("OAuth account configuration is incomplete")
        self._oauth_client = oauth_client
        self._credential_store = credential_store
        self._account_id = account_id
        self._provider = provider
        self._client_id = client_id
        self._tenant = tenant
        self._access_token: AccessToken | None = None

    def get_access_token(self, account_id: str) -> AccessToken:
        """Return a token valid beyond the 120-second connection margin."""

        if account_id != self._account_id:
            raise ConfigError("OAuth token provider was used for a different account")
        now = datetime.now(UTC)
        if self._access_token is not None and self._access_token.expires_at > now + timedelta(
            seconds=120
        ):
            return self._access_token

        refresh_token = self._credential_store.get_secret(account_id, "refresh_token")
        if not refresh_token:
            raise AuthenticationError("OAuth account requires reauthorization")
        client_secret = self._credential_store.get_secret(account_id, "client_secret")
        response = self._oauth_client.refresh_access_token(
            self._provider,
            self._client_id,
            refresh_token,
            client_secret,
            tenant=self._tenant,
            on_refresh_token_rotated=lambda value: self._credential_store.set_secret(
                account_id, "refresh_token", value
            ),
        )
        self._access_token = AccessToken(response.access_token, response.expires_at)
        return self._access_token


def _provider(provider: str, tenant: str | None) -> _Provider:
    if provider == "google":
        if tenant is not None:
            raise ConfigError("Google OAuth does not accept a tenant")
        return _Provider(
            authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
            token_endpoint="https://oauth2.googleapis.com/token",
            allowed_hosts=frozenset({"accounts.google.com", "oauth2.googleapis.com"}),
            scopes=("https://mail.google.com/",),
            loopback_redirect_host="127.0.0.1",
        )
    if provider == "microsoft":
        tenant_value = "organizations" if tenant is None else tenant
        if tenant_value not in {"consumers", "organizations"}:
            try:
                tenant_value = str(UUID(tenant_value))
            except (ValueError, AttributeError) as error:
                raise ConfigError(
                    "Microsoft tenant must be consumers, organizations, or a tenant ID"
                ) from error
        tenant_path = urllib.parse.quote(tenant_value, safe="")
        endpoint_base = f"https://login.microsoftonline.com/{tenant_path}/oauth2/v2.0"
        return _Provider(
            authorization_endpoint=f"{endpoint_base}/authorize",
            token_endpoint=f"{endpoint_base}/token",
            allowed_hosts=frozenset({"login.microsoftonline.com"}),
            scopes=("https://outlook.office.com/IMAP.AccessAsUser.All", "offline_access"),
            loopback_redirect_host="localhost",
            microsoft=True,
        )
    raise ConfigError("unsupported OAuth provider")


def _redirect_uri(host: str, port: int) -> str:
    return f"http://{host}:{port}/"


def _validate_endpoint(url: str, allowed_hosts: frozenset[str]) -> None:
    parsed = urllib.parse.urlsplit(url)
    try:
        port = parsed.port
    except ValueError as error:
        raise ConfigError("OAuth endpoint has an invalid port") from error
    del port
    if (
        parsed.scheme != "https"
        or parsed.hostname not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.query
    ):
        raise ConfigError("OAuth endpoint is not an approved HTTPS endpoint")


__all__ = ["OAuth2Client", "OAuthAccessTokenProvider"]
