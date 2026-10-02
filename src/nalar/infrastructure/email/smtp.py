import asyncio
import hashlib
import re
import ssl
from collections.abc import Callable
from contextlib import suppress
from email import policy
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import format_datetime

from aiosmtplib import SMTP, SMTPException, SMTPResponseException

from nalar.application.ports.mailer import BeforeSubmit, MailerError, MailSubmission, SenderAction


class DisabledMailer:
    enabled = False
    sender_key = None

    async def send(self, submission: MailSubmission, *, before_submit: BeforeSubmit) -> bool:
        raise MailerError(category="disabled")


def _recipient(value: str) -> str:
    if "\r" in value or "\n" in value:
        raise ValueError("Invalid recipient")
    address = Address(addr_spec=value)
    if not address.username or not address.domain or not address.username.isascii():
        raise ValueError("Invalid recipient")
    domain = address.domain.encode("idna").decode("ascii")
    if not re.fullmatch(r"[A-Za-z0-9.-]+", domain):
        raise ValueError("Invalid recipient")
    return Address(username=address.username, domain=domain).addr_spec


class GmailSmtpMailer:
    enabled = True

    def __init__(
        self,
        username: str,
        password: str,
        from_name: str,
        *,
        command_timeout_s: float,
        data_timeout_s: float,
        total_timeout_s: float,
        smtp_factory: Callable[[], SMTP] | None = None,
    ) -> None:
        self._username = username
        self._password = password
        self._from_name = from_name
        self._data_timeout = data_timeout_s
        self._total_timeout = total_timeout_s
        self.sender_key = hashlib.sha256(username.lower().encode()).hexdigest()
        self._smtp_factory = smtp_factory or (
            lambda: SMTP(
                hostname="smtp.gmail.com",
                port=465,
                use_tls=True,
                start_tls=False,
                validate_certs=True,
                timeout=command_timeout_s,
            )
        )

    def _serialize(self, submission: MailSubmission, recipient: str) -> bytes:
        message = EmailMessage(policy=policy.SMTP)
        message["From"] = Address(display_name=self._from_name, addr_spec=self._username)
        message["To"] = recipient
        message["Subject"] = submission.subject
        message["Message-ID"] = submission.message_id
        message["Date"] = format_datetime(submission.prepared_at)
        message.set_content(submission.text, charset="utf-8", cte="quoted-printable")
        return message.as_bytes()

    async def send(self, submission: MailSubmission, *, before_submit: BeforeSubmit) -> bool:
        try:
            recipient = _recipient(submission.recipient)
            mime = self._serialize(submission, recipient)
        except (ValueError, UnicodeError):
            raise MailerError(category="invalid_message") from None
        smtp = self._smtp_factory()
        submitting = False
        authenticating = False
        try:
            async with asyncio.timeout(self._total_timeout):
                await smtp.connect()
                await smtp.ehlo()
                authenticating = True
                await smtp.login(self._username, self._password)
                authenticating = False
                await smtp.mail(self._username)
                await smtp.rcpt(recipient)
                if not await before_submit():
                    return False
                submitting = True
                response = await smtp.data(mime, timeout=self._data_timeout)
                if response.code != 250:
                    raise MailerError(acceptance_unknown=True, category="unexpected_reply")
                return True
        except ssl.SSLCertVerificationError:
            raise MailerError(
                sender_action="suspend", category="tls_verification", acceptance_unknown=submitting
            ) from None
        except SMTPResponseException as exc:
            if submitting and not 400 <= exc.code < 600:
                raise MailerError(acceptance_unknown=True, category="unexpected_reply") from None
            match = re.search(r"\b([245]\.\d{1,3}\.\d{1,3})\b", str(exc.message))
            enhanced = match.group(1) if match else None
            action: SenderAction = "none"
            category = "smtp_rejected"
            if exc.code == 550 and enhanced == "5.4.5":
                action, category = "cooldown", "quota"
            elif authenticating:
                action = "cooldown" if 400 <= exc.code < 500 else "suspend"
                category = "authentication"
            raise MailerError(
                retryable=400 <= exc.code < 500 or action != "none",
                sender_action=action,
                category=category,
                smtp_code=exc.code,
                enhanced_code=enhanced,
            ) from None
        except (OSError, TimeoutError, SMTPException):
            raise MailerError(
                retryable=not submitting,
                acceptance_unknown=submitting,
                category="acceptance_unknown" if submitting else "transport",
            ) from None
        finally:
            with suppress(Exception):
                smtp.close()
