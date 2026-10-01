from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from nalar.application.ports.ai_contract import (
    ClassInsightIn,
    ClassInsightOut,
    EmbedIn,
    EmbedOut,
    EvaluateIn,
    EvaluateOut,
    InvocationOut,
    NextTurnIn,
    NextTurnOut,
    ParentSummaryIn,
    ParentSummaryOut,
    WarmIn,
    WarmOut,
)


@dataclass(frozen=True)
class AiResult[T]:
    result: T
    invocations: list[InvocationOut]
    warnings: list[str] = field(default_factory=list)


class AiServiceError(Exception):
    """An AI call failed. `invocations` are the paid attempts and must still be persisted."""

    def __init__(
        self,
        code: str,
        http_status: int | None,
        invocations: Sequence[InvocationOut] = (),
        message: str = "",
    ) -> None:
        self.code = code
        self.http_status = http_status
        self.invocations = list(invocations)
        self.message = message
        super().__init__(f"{code} ({http_status}): {message}")


class AiGateway(Protocol):
    async def next_turn(self, body: NextTurnIn, request_id: str) -> AiResult[NextTurnOut]: ...

    async def warm_run(self, body: WarmIn, request_id: str) -> AiResult[WarmOut]: ...

    async def evaluate_session(
        self, body: EvaluateIn, request_id: str
    ) -> AiResult[EvaluateOut]: ...

    async def embed(self, body: EmbedIn, request_id: str) -> AiResult[EmbedOut]: ...

    async def class_insight(
        self, body: ClassInsightIn, request_id: str
    ) -> AiResult[ClassInsightOut]: ...

    async def parent_summary(
        self, body: ParentSummaryIn, request_id: str
    ) -> AiResult[ParentSummaryOut]: ...


class AiInvocationLog(Protocol):
    async def record(
        self, school_id: UUID | None, invocations: Sequence[InvocationOut]
    ) -> list[UUID]:
        """Persist every invocation, successful or failed; return the new row ids in order."""
        ...
