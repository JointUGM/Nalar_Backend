from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from nalar.domain.class_map import StudentAttempt


@dataclass(frozen=True)
class MonitorRun:
    id: UUID
    mode: str
    status: str
    join_code: str | None
    started_at: datetime | None


@dataclass(frozen=True)
class MonitorStudent:
    student_id: UUID
    name: str
    status: str
    current_turn_index: int | None
    deadline_at: datetime | None
    open_flag_count: int
    safety_paused: bool


@dataclass(frozen=True)
class Monitor:
    run: MonitorRun
    max_turns: int
    waiting_count: int
    students: tuple[MonitorStudent, ...]


@dataclass(frozen=True)
class ReportTurn:
    turn_id: UUID
    turn_index: int
    kind: str
    prompt: str
    answer: str | None
    move: str | None
    move_source: str | None
    reason_code: str | None
    reason: str | None
    guard_result: str | None
    answer_state: str | None
    safety_paused: bool


@dataclass(frozen=True)
class ReportOverride:
    previous_level: int
    new_level: int
    reason: str
    created_at: datetime


@dataclass(frozen=True)
class ReportScore:
    score_id: UUID
    dimension: str
    ai_level: int
    final_level: int
    rationale: str | None
    evidence: tuple[tuple[UUID, str], ...]
    overrides: tuple[ReportOverride, ...]


@dataclass(frozen=True)
class ReportConceptResult:
    concept_id: UUID
    outcome: str
    misconception_id: UUID | None
    resolved_in_session: bool


@dataclass(frozen=True)
class ReportFlag:
    id: UUID
    flag_type: str
    severity: str
    status: str


@dataclass(frozen=True)
class SessionReport:
    student_id: UUID
    student_name: str
    status: str
    end_reason: str | None
    started_at: datetime
    ended_at: datetime | None
    attempt_number: int
    evaluation_status: str | None
    evaluation_summary: str | None
    turns: tuple[ReportTurn, ...]
    scores: tuple[ReportScore, ...]
    concept_results: tuple[ReportConceptResult, ...]
    flags: tuple[ReportFlag, ...]


@dataclass(frozen=True)
class ClassMapInput:
    concepts: tuple[tuple[UUID, str], ...]
    misconceptions: tuple[tuple[UUID, UUID, str], ...]
    attempts: tuple[StudentAttempt, ...]


class ResultsRepo(Protocol):
    async def monitor(self, publication_id: UUID) -> Monitor | None:
        """Every actively enrolled student of the class, with their latest attempt."""
        ...

    async def report(self, session_id: UUID) -> SessionReport | None: ...

    async def class_map_input(self, publication_id: UUID) -> ClassMapInput | None:
        """Targets as (id, name), misconceptions as (id, concept_id, statement), and each
        student's latest attempt only."""
        ...
