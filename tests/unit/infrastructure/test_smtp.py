import asyncio
import ssl
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from typing import cast

import pytest
from aiosmtplib import SMTP, SMTPDataError, SMTPResponse

from nalar.application.ports.mailer import MailerError, MailSubmission
from nalar.infrastructure.email.smtp import GmailSmtpMailer


class ScriptedSmtp:
    def __init__(self, *, stage: str = "", code: int = 0) -> None:
        self.stage = stage
        self.code = code
        self.messages: list[bytes] = []
        self.closed = False

    async def connect(self) -> SMTPResponse:
        if self.stage == "certificate":
            raise ssl.SSLCertVerificationError("untrusted certificate")
        if self.stage == "connect":
            raise TimeoutError("secret transport details")
        return SMTPResponse(220, "ok")

    async def ehlo(self) -> SMTPResponse:
        return SMTPResponse(250, "ok")

    async def login(self, username: str, password: str) -> SMTPResponse:
        if self.stage == "auth":
            raise SMTPDataError(self.code, "private account details")
        return SMTPResponse(235, "ok")

    async def mail(self, sender: str) -> SMTPResponse:
        return SMTPResponse(250, "ok")

    async def rcpt(self, recipient: str) -> SMTPResponse:
        return SMTPResponse(250, "ok")

    async def data(self, message: bytes, **kwargs: float) -> SMTPResponse:
        if self.code and self.stage == "data":
            raise SMTPDataError(self.code, "5.4.5 quota" if self.code == 550 else "private reply")
        self.messages.append(message)
        if self.stage == "cancel":
            raise asyncio.CancelledError()
        if self.stage == "lost_reply":
            raise TimeoutError("accepted but reply lost")
        return SMTPResponse(250, "accepted")

    def close(self) -> None:
        self.closed = True
        if self.stage == "close":
            raise OSError("disconnect after acceptance")


SUBMISSION = MailSubmission(
    "parent@example.test",
    "Ringkasan NALAR",
    "Ananda â€” pemahaman gaya gesek.",
    "<stable-id@gmail.com>",
    datetime(2026, 10, 2, tzinfo=UTC),
)


def adapter(peer: ScriptedSmtp) -> GmailSmtpMailer:
    return GmailSmtpMailer(
        "nalar.test@gmail.com",
        "abcdefghijklmnop",
        "NALAR",
        command_timeout_s=10,
        data_timeout_s=60,
        total_timeout_s=90,
        smtp_factory=lambda: cast(SMTP, peer),
    )


async def permit() -> bool:
    return True


@pytest.mark.parametrize("stage,unknown", [("connect", False), ("lost_reply", True)])
async def test_transport_failure_classifies_acceptance(stage: str, unknown: bool) -> None:
    peer = ScriptedSmtp(stage=stage)
    with pytest.raises(MailerError) as caught:
        await adapter(peer).send(SUBMISSION, before_submit=permit)
    assert caught.value.acceptance_unknown is unknown
    assert caught.value.retryable is not unknown
    assert "private" not in str(caught.value)
    assert peer.closed


async def test_declined_guard_never_transfers_body() -> None:
    peer = ScriptedSmtp()

    async def decline() -> bool:
        return False

    assert not await adapter(peer).send(SUBMISSION, before_submit=decline)
    assert not peer.messages


@pytest.mark.parametrize(
    "stage,code,action",
    [
        ("data", 451, "none"),
        ("data", 550, "cooldown"),
        ("data", 554, "none"),
        ("auth", 535, "suspend"),
        ("auth", 454, "cooldown"),
    ],
)
async def test_explicit_refusal_is_not_unknown(stage: str, code: int, action: str) -> None:
    with pytest.raises(MailerError) as caught:
        await adapter(ScriptedSmtp(stage=stage, code=code)).send(SUBMISSION, before_submit=permit)
    assert not caught.value.acceptance_unknown
    assert caught.value.sender_action == action


async def test_close_failure_cannot_reverse_acceptance_and_unicode_survives() -> None:
    peer = ScriptedSmtp(stage="close")
    assert await adapter(peer).send(SUBMISSION, before_submit=permit)
    [raw] = peer.messages
    message = BytesParser(policy=policy.default).parsebytes(raw)
    assert message["From"] == "NALAR <nalar.test@gmail.com>"
    assert message["Message-ID"] == SUBMISSION.message_id
    assert SUBMISSION.text in message.get_content()


@pytest.mark.parametrize(
    "recipient", ["a@example.test\r\nBcc: leak@example.test", "a@x.test,b@x.test"]
)
async def test_invalid_recipient_never_connects_or_submits(recipient: str) -> None:
    peer = ScriptedSmtp()
    invalid = MailSubmission(recipient, "Digest", "text", "<id@gmail.com>", SUBMISSION.prepared_at)
    with pytest.raises(MailerError) as caught:
        await adapter(peer).send(invalid, before_submit=permit)
    assert not caught.value.retryable
    assert not peer.messages


async def test_untrusted_certificate_pauses_sender_without_transferring_body() -> None:
    peer = ScriptedSmtp(stage="certificate")
    with pytest.raises(MailerError) as caught:
        await adapter(peer).send(SUBMISSION, before_submit=permit)
    assert caught.value.sender_action == "suspend"
    assert not caught.value.acceptance_unknown
    assert not peer.messages


async def test_cancellation_closes_connection_and_does_not_hide_cancellation() -> None:
    peer = ScriptedSmtp(stage="cancel")
    with pytest.raises(asyncio.CancelledError):
        await adapter(peer).send(SUBMISSION, before_submit=permit)
    assert peer.closed


async def test_unexpected_data_reply_is_unknown_acceptance() -> None:
    with pytest.raises(MailerError) as caught:
        await adapter(ScriptedSmtp(stage="data", code=354)).send(SUBMISSION, before_submit=permit)
    assert caught.value.acceptance_unknown


async def test_total_deadline_bounds_a_stalled_data_transfer() -> None:
    class StalledSmtp(ScriptedSmtp):
        async def data(self, message: bytes, **kwargs: float) -> SMTPResponse:
            await asyncio.Event().wait()
            return SMTPResponse(250, "accepted")

    peer = StalledSmtp()
    mailer = GmailSmtpMailer(
        "nalar.test@gmail.com",
        "abcdefghijklmnop",
        "NALAR",
        command_timeout_s=0.01,
        data_timeout_s=0.02,
        total_timeout_s=0.03,
        smtp_factory=lambda: cast(SMTP, peer),
    )
    with pytest.raises(MailerError) as caught:
        await mailer.send(SUBMISSION, before_submit=permit)
    assert caught.value.acceptance_unknown and peer.closed
