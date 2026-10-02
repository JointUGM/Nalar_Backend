import secrets
from datetime import datetime
from typing import Any
from uuid import UUID

import httpx

from nalar.application.ports.auth_admin import AuthAccount, AuthAdminError

_PAGE_SIZE = 200


class SupabaseAuthAdmin:
    """Supabase Auth Admin API over httpx (B11). The client carries the service-role headers."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

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
            UUID(user["id"]), datetime.fromisoformat(signed_in) if signed_in else None
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
