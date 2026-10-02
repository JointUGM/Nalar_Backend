import asyncio
import math
import secrets
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from uuid import UUID

import httpx

from nalar.application.ports.auth_admin import AuthAccount, AuthAdminError, AuthEmailError

_PAGE_SIZE = 200


class SupabaseAuthAdmin:
    """Supabase Auth Admin API over httpx (B11). The client carries the service-role headers."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        email_request_timeout_s: float = 10,
        email_total_timeout_s: float = 15,
    ) -> None:
        self._http = http
        self._email_request_timeout_s = email_request_timeout_s
        self._email_total_timeout_s = email_total_timeout_s

    async def get_account(self, user_id: UUID) -> AuthAccount | None:
        try:
            response = await self._http.get(f"/auth/v1/admin/users/{user_id}")
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return self._account(response.json())
        except httpx.HTTPStatusError as exc:
            raise AuthAdminError(
                retryable=exc.response.status_code >= 500 or exc.response.status_code in (408, 429)
            ) from exc
        except httpx.HTTPError as exc:
            raise AuthAdminError(retryable=True) from exc
        except (ValueError, KeyError, TypeError) as exc:
            raise AuthAdminError(retryable=False) from exc

    async def send_setup_email(self, email: str, redirect_to: str) -> None:
        try:
            async with asyncio.timeout(self._email_total_timeout_s):
                response = await self._http.post(
                    "/auth/v1/recover",
                    params={"redirect_to": redirect_to},
                    json={"email": email},
                    timeout=self._email_request_timeout_s,
                )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise AuthEmailError("connect", retryable=True) from exc
        except (httpx.HTTPError, TimeoutError) as exc:
            raise AuthEmailError("transport", acceptance_unknown=True) from exc
        if response.is_success:
            return
        if response.status_code == 429:
            raise AuthEmailError(
                "rate_limited", retryable=True, retry_after_s=self._retry_after(response)
            )
        if response.status_code in (401, 403):
            raise AuthEmailError("auth")
        # SMTP failures can also surface as HTTP 400 after token/mail processing.
        raise AuthEmailError("provider_error", acceptance_unknown=True)

    def _retry_after(self, response: httpx.Response) -> float | None:
        value = response.headers.get("Retry-After")
        if value is None:
            return None
        try:
            seconds = float(value)
        except ValueError:
            try:
                when = parsedate_to_datetime(value)
                if when.tzinfo is None:
                    return None
                seconds = (when - datetime.now(UTC)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                return None
        return max(0, seconds) if math.isfinite(seconds) else None

    async def create_or_find(self, email: str, full_name: str) -> AuthAccount:
        try:
            return await self._ensure_account(email, secrets.token_urlsafe(32), full_name)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise AuthAdminError(retryable=status in (408, 429) or status >= 500) from exc
        except httpx.HTTPError as exc:
            raise AuthAdminError(retryable=True) from exc
        except LookupError as exc:
            raise AuthAdminError(retryable=False) from exc

    async def ensure_user(self, email: str, password: str, full_name: str) -> UUID:
        return (await self._ensure_account(email, password, full_name)).id

    async def _ensure_account(self, email: str, password: str, full_name: str) -> AuthAccount:
        response = await self._http.post(
            "/auth/v1/admin/users",
            json={
                "email": email,
                "password": password,
                "email_confirm": True,
                "user_metadata": {"full_name": full_name},
            },
        )
        if response.status_code in (200, 201):
            return self._account(response.json())
        if response.status_code in (409, 422):
            return await self._find(email)
        response.raise_for_status()
        raise RuntimeError(f"unexpected auth admin status {response.status_code}")

    def _account(self, user: dict[str, Any]) -> AuthAccount:
        signed_in = user.get("last_sign_in_at")
        return AuthAccount(
            UUID(user["id"]),
            datetime.fromisoformat(signed_in) if signed_in else None,
            user.get("email"),
        )

    async def _find(self, email: str) -> AuthAccount:
        page = 1
        while True:
            response = await self._http.get(
                "/auth/v1/admin/users", params={"page": page, "per_page": _PAGE_SIZE}
            )
            response.raise_for_status()
            users = response.json().get("users", [])
            for user in users:
                if str(user.get("email", "")).lower() == email.lower():
                    return self._account(user)
            if len(users) < _PAGE_SIZE:
                raise LookupError("Auth user not found")
            page += 1
