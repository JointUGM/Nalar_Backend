from datetime import UTC, datetime
from uuid import uuid4

import httpx

from nalar.infrastructure.auth.admin import SupabaseAuthAdmin


async def test_existing_account_preserves_sign_in_history_without_password_update() -> None:
    user_id = uuid4()
    calls: list[tuple[str, str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(422)
        return httpx.Response(
            200,
            json={
                "users": [
                    {
                        "id": str(user_id),
                        "email": "used@example.com",
                        "last_sign_in_at": "2026-10-01T09:00:00Z",
                    }
                ]
            },
        )

    async with httpx.AsyncClient(
        base_url="http://auth.test", transport=httpx.MockTransport(respond)
    ) as http:
        account = await SupabaseAuthAdmin(http).create_or_find("used@example.com", "Guru")
    assert account.id == user_id
    assert account.last_sign_in_at == datetime(2026, 10, 1, 9, tzinfo=UTC)
    assert calls == [("POST", "/auth/v1/admin/users"), ("GET", "/auth/v1/admin/users")]
