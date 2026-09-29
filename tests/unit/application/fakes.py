from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel

from nalar.application.ports.ai import AiResult, AiServiceError
from nalar.application.ports.ai_contract import (
    EmbedIn,
    EmbedOut,
    EvaluateIn,
    EvaluateOut,
    InvocationOut,
    NextTurnIn,
    NextTurnOut,
    WarmIn,
    WarmOut,
)

type Reply = AiResult[Any] | AiServiceError


class FakeClock:
    def __init__(self, now: datetime | None = None) -> None:
        self.current = now or datetime.now(UTC).replace(microsecond=0)

    def now(self) -> datetime:
        return self.current

    def advance(self, **delta: float) -> None:
        self.current += timedelta(**delta)


def invocation(purpose: str, status: str = "success") -> InvocationOut:
    return InvocationOut.model_validate(
        {
            "purpose": purpose,
            "model": "test-model",
            "prompt_version": "test.v1",
            "provider": "sumopod",
            "status": status,
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "latency_ms": 100,
            "cost_usd": 0.0001,
            "request_id": "test",
            "retrieval": [],
            "error_message": None if status == "success" else "failed",
        }
    )


class ScriptedAiGateway:
    def __init__(self) -> None:
        self.replies: dict[str, list[Reply]] = defaultdict(list)
        self.calls: list[tuple[str, BaseModel]] = []

    def script(self, method: str, *replies: Reply) -> None:
        self.replies[method].extend(replies)

    async def _reply(self, method: str, body: BaseModel) -> AiResult[Any]:
        self.calls.append((method, body))
        if not self.replies[method]:
            raise AiServiceError("unscripted", None, message=method)
        reply = self.replies[method].pop(0)
        if isinstance(reply, AiServiceError):
            raise reply
        return reply

    async def next_turn(self, body: NextTurnIn, request_id: str) -> AiResult[NextTurnOut]:
        return cast(AiResult[NextTurnOut], await self._reply("next_turn", body))

    async def warm_run(self, body: WarmIn, request_id: str) -> AiResult[WarmOut]:
        return cast(AiResult[WarmOut], await self._reply("warm_run", body))

    async def evaluate_session(self, body: EvaluateIn, request_id: str) -> AiResult[EvaluateOut]:
        return cast(AiResult[EvaluateOut], await self._reply("evaluate_session", body))

    async def embed(self, body: EmbedIn, request_id: str) -> AiResult[EmbedOut]:
        return cast(AiResult[EmbedOut], await self._reply("embed", body))


class RecordingBackground:
    def __init__(self) -> None:
        self.warmed: list[UUID] = []
        self.turn_steps: list[tuple[UUID, int]] = []

    def warm_run(self, run_id: UUID) -> None:
        self.warmed.append(run_id)

    def run_turn_step(self, session_id: UUID, answered_turn_index: int) -> None:
        self.turn_steps.append((session_id, answered_turn_index))
