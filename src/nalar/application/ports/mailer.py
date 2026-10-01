from typing import Protocol


class MailerError(Exception):
    def __init__(self, *, retryable: bool = True) -> None:
        super().__init__("Email delivery failed")
        self.retryable = retryable


class Mailer(Protocol):
    @property
    def enabled(self) -> bool: ...

    async def send(self, to: str, subject: str, text: str, *, idempotency_key: str) -> None:
        """Send an immutable request, reusing the key after ambiguous failures."""
        ...
