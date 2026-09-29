import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

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
from nalar.application.ports.sessions import TurnContext
from nalar.application.ports.turns import NewTurn, TurnAnalysis
from nalar.domain.labels import SessionEndReason, SessionStatus
from nalar.domain.sessions import OPEN_STATUSES

type Reply = AiResult[Any] | AiServiceError

SEED_PACK: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[3] / "supabase/seed/gaya_dan_gerak.json").read_text(
        encoding="utf-8"
    )
)["pack"]


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


@dataclass
class FakeSessions:
    context: TurnContext | None = None
    status: SessionStatus = SessionStatus.in_progress
    ended: list[tuple[SessionStatus, SessionEndReason]] = field(default_factory=list)

    async def turn_context(self, session_id: UUID) -> TurnContext | None:
        return replace(self.context, status=self.status) if self.context else None

    async def lock_if_in_progress(self, session_id: UUID) -> bool:
        return self.status is SessionStatus.in_progress

    async def end(
        self, session_id: UUID, status: SessionStatus, reason: SessionEndReason, now: datetime
    ) -> bool:
        if self.status is not SessionStatus.in_progress:
            return False
        self.status = status
        self.ended.append((status, reason))
        return True

    async def pause_for_safety(self, session_id: UUID) -> bool:
        if self.status is not SessionStatus.in_progress:
            return False
        self.status = SessionStatus.paused_safety
        return True

    async def time_out(self, session_id: UUID, now: datetime) -> bool:
        assert self.context is not None
        if self.status not in OPEN_STATUSES or now < self.context.deadline_at:
            return False
        self.status = SessionStatus.timed_out
        self.ended.append((SessionStatus.timed_out, SessionEndReason.max_duration_reached))
        return True


@dataclass
class FakeTurns:
    analyses: dict[UUID, TurnAnalysis] = field(default_factory=dict)
    appended: list[NewTurn] = field(default_factory=list)

    async def record_analysis(self, turn_id: UUID, analysis: TurnAnalysis) -> None:
        self.analyses[turn_id] = analysis

    async def exists(self, session_id: UUID, turn_index: int) -> bool:
        return any(t.turn_index == turn_index for t in self.appended)

    async def append(self, session_id: UUID, school_id: UUID, turn: NewTurn) -> bool:
        if await self.exists(session_id, turn.turn_index):
            return False
        self.appended.append(turn)
        return True


@dataclass
class FakeNotifications:
    alerts: set[tuple[UUID, int]] = field(default_factory=set)

    async def wellbeing_alert(
        self,
        recipient_ids: list[UUID],
        school_id: UUID,
        session_id: UUID,
        publication_id: UUID,
        turn_index: int,
    ) -> int:
        new = {(r, turn_index) for r in recipient_ids} - self.alerts
        self.alerts |= new
        return len(new)


@dataclass
class FakePublications:
    teachers: list[UUID] = field(default_factory=list)

    async def teacher_ids(self, publication_id: UUID) -> list[UUID]:
        return self.teachers


@dataclass
class FakeQueue:
    sent: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def send(self, queue: str, body: dict[str, Any], delay_s: int = 0) -> int:
        self.sent.append((queue, body))
        return len(self.sent)


@dataclass
class FakeInvocationLog:
    recorded: list[InvocationOut] = field(default_factory=list)

    async def record(
        self, school_id: UUID | None, invocations: Sequence[InvocationOut]
    ) -> list[UUID]:
        self.recorded.extend(invocations)
        return [uuid4() for _ in invocations]


class FakeUnitOfWork:
    """Only the parts the turn step touches; handlers are typed against UnitOfWork, tests cast."""

    def __init__(self) -> None:
        self.sessions = FakeSessions()
        self.turns = FakeTurns()
        self.notifications = FakeNotifications()
        self.publications = FakePublications()
        self.queue = FakeQueue()
        self.ai_invocations = FakeInvocationLog()

    async def __aenter__(self) -> "FakeUnitOfWork":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None
