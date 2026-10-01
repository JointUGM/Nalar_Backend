import httpx

from nalar.application.ports.mailer import MailerError


class DisabledMailer:
    enabled = False

    async def send(self, to: str, subject: str, text: str, *, idempotency_key: str) -> None:
        raise MailerError(retryable=False)


class ResendMailer:
    enabled = True

    def __init__(self, http: httpx.AsyncClient, sender: str) -> None:
        self._http = http
        self._sender = sender

    async def send(self, to: str, subject: str, text: str, *, idempotency_key: str) -> None:
        try:
            response = await self._http.post(
                "/emails",
                headers={"Idempotency-Key": idempotency_key},
                json={"from": self._sender, "to": [to], "subject": subject, "text": text},
            )
        except httpx.HTTPError as exc:
            raise MailerError() from exc
        if response.is_error:
            raise MailerError(
                retryable=response.status_code in (408, 409, 429) or response.status_code >= 500
            )
