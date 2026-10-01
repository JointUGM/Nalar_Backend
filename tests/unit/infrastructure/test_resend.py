import json

import httpx
import pytest

from nalar.application.ports.mailer import MailerError
from nalar.infrastructure.email.resend import ResendMailer


@pytest.mark.parametrize("status,retryable", [(200, None), (429, True), (503, True), (422, False)])
async def test_resend_preserves_delivery_key_and_classifies_failures(
    status: int,
    retryable: bool | None,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["Idempotency-Key"] == "weekly:parent:2026-W40"
        assert json.loads(request.content) == {
            "from": "NALAR <digest@example.test>",
            "to": ["parent@example.test"],
            "subject": "Weekly digest",
            "text": "Released summary",
        }
        return httpx.Response(status)

    async with httpx.AsyncClient(
        base_url="https://api.resend.com",
        transport=httpx.MockTransport(respond),
    ) as http:
        mailer = ResendMailer(http, "NALAR <digest@example.test>")
        if retryable is None:
            await mailer.send(
                "parent@example.test",
                "Weekly digest",
                "Released summary",
                idempotency_key="weekly:parent:2026-W40",
            )
        else:
            with pytest.raises(MailerError) as caught:
                await mailer.send(
                    "parent@example.test",
                    "Weekly digest",
                    "Released summary",
                    idempotency_key="weekly:parent:2026-W40",
                )
            assert caught.value.retryable is retryable


async def test_resend_timeout_remains_retryable() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("delivery outcome unknown", request=request)

    async with httpx.AsyncClient(
        base_url="https://api.resend.com",
        transport=httpx.MockTransport(timeout),
    ) as http:
        with pytest.raises(MailerError) as caught:
            await ResendMailer(http, "digest@example.test").send(
                "parent@example.test",
                "Weekly digest",
                "Released summary",
                idempotency_key="weekly:parent:2026-W40",
            )
        assert caught.value.retryable
