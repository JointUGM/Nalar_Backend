from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from nalar.application.ports.administration import AdminRow
from nalar.domain.class_map import StudentAttempt


@dataclass(frozen=True)
class MonitorRun:
    id: UUID
    mode: str
    status: str
    join_code: str | None
    started_at: datetime | None


@dataclass(frozen=True)
class MonitorFlag:
    id: UUID
    flag_type: str
    severity: str
    turn_index: int | None
    created_at: datetime


@dataclass(frozen=True)
class MonitorStudent:
    student_id: UUID
    name: str
    status: str
    current_turn_index: int | None
    deadline_at: datetime | None
    open_flag_count: int
    safety_paused: bool
    session_id: UUID | None = None
    participant_id: UUID | None = None
    open_flags: tuple[MonitorFlag, ...] = ()


@dataclass(frozen=True)
class Monitor:
    run: MonitorRun
    max_turns: int
    waiting_count: int
    students: tuple[MonitorStudent, ...]


@dataclass(frozen=True)
class ReportActivity:
    paste_chars: int
    away_seconds: float
    typing_ms: int


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
    activity: ReportActivity


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
    turn_index: int | None
    created_at: datetime
    evidence: dict[str, Any] | None = None


@dataclass(frozen=True)
class ReportMission:
    mission_id: UUID
    title: str
    version_number: int


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
    mission: ReportMission
    rubric: dict[str, list[str]]


@dataclass(frozen=True)
class ClassMapStudent:
    student_id: UUID
    name: str
    session_id: UUID


@dataclass(frozen=True)
class ClassMapInput:
    concepts: tuple[tuple[UUID, str], ...]
    misconceptions: tuple[tuple[UUID, UUID, str], ...]
    attempts: tuple[StudentAttempt, ...]
    students: tuple[ClassMapStudent, ...]
    prerequisites: tuple[tuple[UUID, UUID], ...] = ()


@dataclass(frozen=True)
class StudentConceptCounts:
    mastered: int
    developing: int
    misconception: int


@dataclass(frozen=True)
class ClassStudent:
    student_id: UUID
    full_name: str
    session_id: UUID | None
    status: str | None
    completed_at: datetime | None
    concept_counts: StudentConceptCounts
    open_flag_count: int
    evaluation_status: str | None


@dataclass(frozen=True)
class AttentionEntry:
    item_id: UUID
    created_at: datetime


@dataclass(frozen=True)
class SafetyAttention(AttentionEntry):
    session_id: UUID
    publication_id: UUID
    student_name: str
    paused_at: datetime | None
    kind: Literal["safety"] = "safety"


@dataclass(frozen=True)
class FlagAttention(AttentionEntry):
    flag_id: UUID
    flag_type: str
    severity: str
    session_id: UUID
    publication_id: UUID
    student_name: str
    kind: Literal["flag"] = "flag"


@dataclass(frozen=True)
class KbReviewAttention(AttentionEntry):
    knowledge_base_id: UUID
    topic_title: str
    pending_concepts: int
    pending_misconceptions: int
    kind: Literal["kb_review"] = "kb_review"


@dataclass(frozen=True)
class ReleaseReadyAttention(AttentionEntry):
    publication_id: UUID
    class_name: str
    mission_title: str
    eligible_count: int
    kind: Literal["release_ready"] = "release_ready"


AttentionItem = SafetyAttention | FlagAttention | KbReviewAttention | ReleaseReadyAttention
AttentionCursor = tuple[datetime, str, UUID]


@dataclass(frozen=True)
class AttentionCounts:
    safety: int
    flag: int
    kb_review: int
    release_ready: int
    total: int


@dataclass(frozen=True)
class DashboardBucket:
    sessions_completed: int
    students: int
    active_misconceptions: int
    concepts_with_misconceptions: int
    changed_mind_rate: float | None
    open_flags: int
    mastered: int
    developing: int
    misconception: int


@dataclass(frozen=True)
class ChangedMisconception:
    misconception_id: UUID
    statement: str
    held: int
    resolved: int


class ResultsRepo(Protocol):
    async def teacher_dashboard(
        self, actor_id: UUID, school_id: UUID, bounds: list[tuple[datetime, datetime]]
    ) -> tuple[list[DashboardBucket], list[ChangedMisconception]]: ...

    async def class_students(
        self, class_id: UUID, publication_id: UUID | None
    ) -> list[ClassStudent] | None:
        """Active enrollments with latest publication attempts, or None for a mismatched class."""
        ...

    async def teacher_attention(
        self, actor_id: UUID, school_id: UUID, limit: int, after: AttentionCursor | None
    ) -> tuple[list[AttentionItem], AttentionCounts]:
        """Scoped queue and complete counts from one database snapshot."""
        ...

    async def monitor(self, publication_id: UUID) -> Monitor | None:
        """Every actively enrolled student of the class, with their latest attempt."""
        ...

    async def report(self, session_id: UUID) -> SessionReport | None: ...

    async def class_map_input(self, publication_id: UUID) -> ClassMapInput | None:
        """Targets as (id, name), misconceptions as (id, concept_id, statement), and each
        student's latest attempt only."""
        ...

    async def student_history(
        self, teacher_id: UUID, student_id: UUID, limit: int, after: tuple[datetime, UUID] | None
    ) -> list[AdminRow]: ...

    async def publication_scores(self, publication_id: UUID) -> list[AdminRow]: ...
