from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

type BeforeSubmit = Callable[[], Awaitable[bool]]
type SenderAction = Literal["none", "cooldown", "suspend"]


@dataclass(frozen=True)
class MailSubmission:
    recipient: str
    subject: str
    text: str
    message_id: str
    prepared_at: datetime


class MailerError(Exception):
    def __init__(
        self,
        *,
        retryable: bool = False,
        acceptance_unknown: bool = False,
        sender_action: SenderAction = "none",
        category: str = "unavailable",
        smtp_code: int | None = None,
        enhanced_code: str | None = None,
    ) -> None:
        super().__init__("Email submission failed")
        self.retryable = retryable
        self.acceptance_unknown = acceptance_unknown
        self.sender_action = sender_action
        self.category = category
        self.smtp_code = smtp_code
        self.enhanced_code = enhanced_code


class Mailer(Protocol):
    @property
    def enabled(self) -> bool: ...

    @property
    def sender_key(self) -> str | None: ...

    async def send(self, submission: MailSubmission, *, before_submit: BeforeSubmit) -> bool:
        """Submit once after the durable guard; True means accepted, False means declined."""
        ...
