from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from nalar.application.features.integrity.commands.compute_live_session_flags import (
    ComputeLiveSessionFlagsHandler,
)
from nalar.application.features.integrity.messages import (
    session_flags_message,
)
from nalar.application.ports.integrity import ActivityIntegrityInput
from nalar.application.ports.queue import DEFAULT_QUEUE
from nalar.domain.integrity import FlagDraft, IntegrityConfig
from nalar.domain.labels import SessionStatus
from nalar.domain.telemetry import TurnMetrics

CFG = IntegrityConfig(
    version=1,
    paste_min_chars=50,
    paste_min_share=0.5,
    paste_severity="medium",
    hidden_ms_per_answer=15000,
    hidden_events_per_session=5,
    tab_severity="low",
    gap_first_min_quality=3,
    gap_next_max_quality=1,
    gap_severity="high",
    disconnect_min_jump=2,
    disconnect_severity="medium",
    similarity_min_jaccard=0.75,
    similarity_min_tokens=5,
    similarity_severity="high",
)


@dataclass
class FakeIntegrityRepo:
    locked: bool = True
    input_data: ActivityIntegrityInput | None = None
    upserted_metrics: dict[UUID, TurnMetrics] = field(default_factory=dict)
    inserted_flags: list[FlagDraft] = field(default_factory=list)

    async def lock_session(self, session_id: UUID) -> bool:
        return self.locked

    async def activity_input(self, session_id: UUID) -> ActivityIntegrityInput | None:
        return self.input_data

    async def upsert_metrics(self, school_id: UUID, metrics: dict[UUID, TurnMetrics]) -> None:
        self.upserted_metrics.update(metrics)

    async def insert_flags(
        self, school_id: UUID, session_id: UUID, flags: Sequence[FlagDraft]
    ) -> int:
        self.inserted_flags.extend(flags)
        return len(flags)


@dataclass
class FakeQueue:
    sent: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def send(self, queue: str, body: dict[str, Any], delay_s: int = 0) -> int:
        self.sent.append((queue, body))
        return len(self.sent)


class FakeLiveUow:
    def __init__(self, integrity: FakeIntegrityRepo, queue: FakeQueue) -> None:
        self.integrity = integrity
        self.queue = queue

    async def __aenter__(self) -> "FakeLiveUow":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


async def test_unlocked_session_returns_zero() -> None:
    repo = FakeIntegrityRepo(locked=False)
    queue = FakeQueue()
    handler = ComputeLiveSessionFlagsHandler(FakeLiveUow(repo, queue), CFG)  # type: ignore[arg-type]
    assert await handler.execute(uuid4()) == 0
    assert len(queue.sent) == 0


async def test_missing_activity_input_returns_zero() -> None:
    repo = FakeIntegrityRepo(locked=True, input_data=None)
    queue = FakeQueue()
    handler = ComputeLiveSessionFlagsHandler(FakeLiveUow(repo, queue), CFG)  # type: ignore[arg-type]
    assert await handler.execute(uuid4()) == 0
    assert len(queue.sent) == 0


async def test_paused_session_does_not_evaluate_live_activity() -> None:
    school_id = uuid4()
    session_id = uuid4()
    turn_id = uuid4()
    data = ActivityIntegrityInput(
        school_id=school_id,
        status=SessionStatus.paused_safety,
        evaluated=False,
        turns=((turn_id, 0, "Jawaban", False),),
        batches=((turn_id, [{"type": "paste", "value": 100}]),),
    )
    repo = FakeIntegrityRepo(locked=True, input_data=data)
    queue = FakeQueue()
    handler = ComputeLiveSessionFlagsHandler(FakeLiveUow(repo, queue), CFG)  # type: ignore[arg-type]
    assert await handler.execute(session_id) == 0
    assert len(repo.inserted_flags) == 0
    assert len(repo.upserted_metrics) == 0
    assert len(queue.sent) == 0


async def test_terminal_evaluated_session_enqueues_final_session_flags() -> None:
    school_id = uuid4()
    session_id = uuid4()
    turn_id = uuid4()
    data = ActivityIntegrityInput(
        school_id=school_id,
        status=SessionStatus.completed,
        evaluated=True,
        turns=((turn_id, 0, "Jawaban", False),),
        batches=(),
    )
    repo = FakeIntegrityRepo(locked=True, input_data=data)
    queue = FakeQueue()
    handler = ComputeLiveSessionFlagsHandler(FakeLiveUow(repo, queue), CFG)  # type: ignore[arg-type]
    assert await handler.execute(session_id) == 0
    assert queue.sent == [(DEFAULT_QUEUE, session_flags_message(session_id))]


async def test_terminal_unevaluated_session_does_not_enqueue_final_message() -> None:
    school_id = uuid4()
    session_id = uuid4()
    turn_id = uuid4()
    data = ActivityIntegrityInput(
        school_id=school_id,
        status=SessionStatus.completed,
        evaluated=False,
        turns=((turn_id, 0, "Jawaban", False),),
        batches=(),
    )
    repo = FakeIntegrityRepo(locked=True, input_data=data)
    queue = FakeQueue()
    handler = ComputeLiveSessionFlagsHandler(FakeLiveUow(repo, queue), CFG)  # type: ignore[arg-type]
    assert await handler.execute(session_id) == 0
    assert len(queue.sent) == 0


async def test_active_session_computes_metrics_and_inserts_live_flags() -> None:
    school_id = uuid4()
    session_id = uuid4()
    turn_id = uuid4()
    answer = "x" * 150
    data = ActivityIntegrityInput(
        school_id=school_id,
        status=SessionStatus.in_progress,
        evaluated=False,
        turns=((turn_id, 0, answer, False),),
        batches=((turn_id, [{"type": "paste", "value": 100}]),),
    )
    repo = FakeIntegrityRepo(locked=True, input_data=data)
    queue = FakeQueue()
    handler = ComputeLiveSessionFlagsHandler(FakeLiveUow(repo, queue), CFG)  # type: ignore[arg-type]
    count = await handler.execute(session_id)
    assert count == 1
    assert len(repo.inserted_flags) == 1
    assert repo.inserted_flags[0].flag_type == "large_paste"
    assert turn_id in repo.upserted_metrics
    assert len(queue.sent) == 0
