import asyncio
import time
from collections.abc import Callable
from typing import Any
from uuid import UUID

import httpx
import jwt

from nalar.application.errors import DependencyUnavailable, Unauthenticated
from nalar.application.ports.auth import AuthUser

_ASYMMETRIC = frozenset({"ES256", "RS256"})


class SupabaseJwtVerifier:
    """Verifies Supabase access tokens locally: JWKS for ES256/RS256, the secret for HS256."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        jwks_url: str,
        audience: str,
        hs256_secret: str | None,
        refresh_cooldown_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = http
        self._jwks_url = jwks_url
        self._audience = audience
        self._hs256_secret = hs256_secret
        self._refresh_cooldown_s = refresh_cooldown_s
        self._clock = clock
        self._keys: dict[str, Any] = {}
        self._last_refresh: float | None = None
        self._refresh_lock = asyncio.Lock()

    async def verify(self, token: str) -> AuthUser:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise Unauthenticated() from exc

        algorithm = header.get("alg")
        key: Any
        if algorithm == "HS256" and self._hs256_secret:
            key = self._hs256_secret
        elif algorithm in _ASYMMETRIC:
            key = await self._signing_key(header.get("kid"))
        else:
            raise Unauthenticated()

        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=[algorithm],
                audience=self._audience,
                options={"require": ["exp", "sub", "aud"]},
            )
            return AuthUser(id=UUID(claims["sub"]))
        except (jwt.PyJWTError, ValueError) as exc:
            raise Unauthenticated() from exc

    async def _signing_key(self, kid: object) -> Any:
        if not isinstance(kid, str):
            raise Unauthenticated()
        if kid not in self._keys:
            # A cold start sends many requests at once; they wait for one fetch instead of
            # failing inside its cooldown.
            async with self._refresh_lock:
                if kid not in self._keys and self._may_refresh():
                    await self._refresh()
        if kid not in self._keys:
            # With no key set ever loaded, the fault is the JWKS endpoint, not the token.
            raise Unauthenticated() if self._keys else DependencyUnavailable()
        return self._keys[kid]

    def _may_refresh(self) -> bool:
        return (
            self._last_refresh is None
            or self._clock() - self._last_refresh >= self._refresh_cooldown_s
        )

    async def _refresh(self) -> None:
        self._last_refresh = self._clock()
        try:
            response = await self._http.get(self._jwks_url, timeout=5.0)
            response.raise_for_status()
            jwk_set = jwt.PyJWKSet.from_dict(response.json())
        except (httpx.HTTPError, jwt.PyJWTError, ValueError) as exc:
            raise DependencyUnavailable() from exc
        self._keys = {jwk.key_id: jwk.key for jwk in jwk_set.keys if jwk.key_id}
