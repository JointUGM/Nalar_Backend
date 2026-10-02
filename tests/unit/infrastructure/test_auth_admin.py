from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from nalar.application.ports.auth_admin import AuthEmailError
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


@pytest.mark.parametrize(
    "status,category,unknown",
    [
        (429, "rate_limited", False),
        (401, "auth", False),
        (500, "provider_error", True),
        (400, "provider_error", True),
    ],
)
async def test_recovery_rejection_distinguishes_safe_retry_from_uncertain_submission(
    status: int, category: str, unknown: bool
) -> None:
    async with httpx.AsyncClient(
        base_url="http://auth.test",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                status, headers={"Retry-After": "3600"}, text="sensitive provider detail"
            )
        ),
    ) as http:
        with pytest.raises(AuthEmailError) as error:
            await SupabaseAuthAdmin(http).send_setup_email(
                "new@example.com", "https://nalar.test/activate?activation_id=123"
            )
    assert error.value.category == category
    assert error.value.acceptance_unknown == unknown
    assert "sensitive" not in str(error.value)
    if status == 429:
        assert error.value.retry_after_s == 3600


@pytest.mark.parametrize(
    "failure,unknown", [(httpx.ConnectTimeout, False), (httpx.ReadTimeout, True)]
)
async def test_recovery_connection_failure_is_safe_but_response_timeout_is_unknown(
    failure: type[httpx.TimeoutException], unknown: bool
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        raise failure("private transport detail", request=request)

    async with httpx.AsyncClient(
        base_url="http://auth.test", transport=httpx.MockTransport(respond)
    ) as http:
        with pytest.raises(AuthEmailError) as error:
            await SupabaseAuthAdmin(http).send_setup_email(
                "new@example.com", "https://nalar.test/activate"
            )
    assert error.value.acceptance_unknown == unknown
    assert error.value.retryable == (not unknown)


async def test_recovery_sends_only_email_and_the_fixed_redirect() -> None:
    import json

    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        base_url="http://auth.test", transport=httpx.MockTransport(respond)
    ) as http:
        await SupabaseAuthAdmin(http).send_setup_email(
            "new@example.com", "https://nalar.test/activate?activation_id=123"
        )
    assert seen[0].url.path == "/auth/v1/recover"
    assert seen[0].url.params["redirect_to"] == "https://nalar.test/activate?activation_id=123"
    assert json.loads(seen[0].content) == {"email": "new@example.com"}
