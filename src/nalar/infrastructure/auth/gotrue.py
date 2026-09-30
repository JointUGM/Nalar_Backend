import logging
from typing import Any
from uuid import UUID

import httpx

from nalar.application.errors import (
    DependencyUnavailable,
    InvalidCredentials,
    TooManyRequests,
    Unauthenticated,
)
from nalar.application.ports.auth import AuthTokens

log = logging.getLogger(__name__)


class SupabaseIdentityProvider:
    """Supabase Auth password, refresh and logout over httpx; the client carries the anon apikey."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def sign_in(self, email: str, password: str) -> AuthTokens:
        response = await self._post(
            "/auth/v1/token",
            params={"grant_type": "password"},
            json={"email": email, "password": password},
        )
        # GoTrue answers bad credentials with 400; a 401 is the gateway rejecting our apikey.
        if response.status_code in (400, 422):
            raise InvalidCredentials()
        return self._tokens(response)

    async def refresh(self, refresh_token: str) -> AuthTokens:
        response = await self._post(
            "/auth/v1/token",
            params={"grant_type": "refresh_token"},
            json={"refresh_token": refresh_token},
        )
        if response.status_code in (400, 403, 404):
            raise Unauthenticated()
        return self._tokens(response)

    async def sign_out(self, access_token: str) -> None:
        response = await self._post(
            "/auth/v1/logout",
            params={"scope": "local"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if response.status_code not in (401, 403, 404):
            _raise_for_status(response)

    async def _post(self, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self._http.post(path, **kwargs)
        except httpx.HTTPError as exc:
            log.warning("supabase auth unreachable: %s %s", path, type(exc).__name__)
            raise DependencyUnavailable() from exc

    def _tokens(self, response: httpx.Response) -> AuthTokens:
        _raise_for_status(response)
        try:
            body = response.json()
            access, refresh = body["access_token"], body["refresh_token"]
            expires_at = body["expires_at"]
            if not (isinstance(access, str) and access and isinstance(refresh, str) and refresh):
                raise ValueError("empty token")
            if not isinstance(expires_at, int):
                raise ValueError("expires_at")
            return AuthTokens(UUID(body["user"]["id"]), access, refresh, expires_at)
        except (ValueError, KeyError, TypeError) as exc:
            log.warning("supabase auth returned an unreadable session: %s", exc)
            raise DependencyUnavailable() from exc


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code == 429:
        raise TooManyRequests()
    if not response.is_success:
        log.warning("supabase auth failed: %s %s", response.request.url.path, response.status_code)
        raise DependencyUnavailable()
