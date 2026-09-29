from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from nalar.domain.labels import EvaluationStatus, SessionStatus


@dataclass(frozen=True)
class EvalTurn:
    id: UUID
    turn_index: int
    kind: str
    question_text: str
    answer_text: str | None
    move: str | None
    target_concept_id: UUID | None


@dataclass(frozen=True)
class EvaluationInput:
    session_id: UUID
    school_id: UUID
    status: SessionStatus
    evaluated: bool
    context_pack: Mapping[str, Any]
    rubric: Mapping[str, Any]
    turns: tuple[EvalTurn, ...]


@dataclass(frozen=True)
class ConceptResultRow:
    concept_id: UUID
    outcome: str
    misconception_id: UUID | None
    initial_misconception_id: UUID | None
    resolved_in_session: bool
    evidence_turn_id: UUID | None


@dataclass(frozen=True)
class ReflectionView:
    status: SessionStatus
    evaluated: bool
    mission_title: str
    ended_at: datetime | None
    content: str | None
    warmup_choice_id: str | None
    live_warmup: Mapping[str, Any] | None


class EvaluationsRepo(Protocol):
    async def evaluation_input(self, session_id: UUID) -> EvaluationInput | None: ...

    async def insert(
        self,
        session_id: UUID,
        school_id: UUID,
        status: EvaluationStatus,
        summary: str | None,
        turn_quality_json: str,
        ai_invocation_id: UUID | None,
    ) -> UUID | None:
        """NFR-R2: None when the session already has its evaluation (the unique session_id)."""
        ...

    async def insert_score(
        self, school_id: UUID, evaluation_id: UUID, dimension: str, level: int, rationale: str
    ) -> UUID: ...

    async def insert_evidence(
        self, school_id: UUID, score_id: UUID, turn_id: UUID, quote: str
    ) -> None: ...

    async def insert_concept_result(
        self, school_id: UUID, session_id: UUID, result: ConceptResultRow
    ) -> None: ...

    async def insert_reflection(
        self, school_id: UUID, session_id: UUID, content: str, ai_invocation_id: UUID | None
    ) -> None:
        """AI-5: stored once and never regenerated."""
        ...

    async def reflection_view(self, session_id: UUID) -> ReflectionView | None: ...
