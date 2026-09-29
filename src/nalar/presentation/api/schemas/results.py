from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from nalar.presentation.api.schemas.common import Body


class MonitorRunOut(BaseModel):
    id: UUID
    mode: str
    status: str
    join_code: str | None
    started_at: datetime | None


class MonitorStudentOut(BaseModel):
    student_id: UUID
    name: str
    status: str
    current_turn_index: int | None
    max_turns: int
    deadline_at: datetime | None
    open_flag_count: int
    safety_paused: bool


class MonitorOut(BaseModel):
    run: MonitorRunOut
    waiting_count: int
    students: list[MonitorStudentOut]


class MisconceptionCountOut(BaseModel):
    misconception_id: UUID
    statement: str
    count: int
    resolved_count: int
    student_ids: list[UUID]


class ConceptCountOut(BaseModel):
    concept_id: UUID
    name: str
    mastered_count: int
    developing_count: int
    not_observed_count: int
    misconceptions: list[MisconceptionCountOut]


class InsightOut(BaseModel):
    narrative: str
    generated_at: datetime


class ClassMapOut(BaseModel):
    denominator: int
    incomplete_count: int
    concepts: list[ConceptCountOut]
    insight: InsightOut | None = None


class ReportStudentOut(BaseModel):
    id: UUID
    name: str


class ReportSessionOut(BaseModel):
    status: str
    end_reason: str | None
    started_at: datetime
    ended_at: datetime | None
    attempt_number: int


class ReportEvaluationOut(BaseModel):
    status: str
    summary: str | None


class ReportTurnOut(BaseModel):
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


class EvidenceOut(BaseModel):
    turn_id: UUID
    quote: str


class OverrideOut(BaseModel):
    previous_level: int
    new_level: int
    reason: str
    created_at: datetime


class ReportScoreOut(BaseModel):
    score_id: UUID
    dimension: str
    ai_level: int
    final_level: int
    rationale: str | None
    evidence: list[EvidenceOut]
    overrides: list[OverrideOut]


class ReportConceptResultOut(BaseModel):
    concept_id: UUID
    outcome: str
    misconception_id: UUID | None
    resolved_in_session: bool


class ReportFlagOut(BaseModel):
    id: UUID
    flag_type: str
    severity: str
    status: str


class ReportOut(BaseModel):
    student: ReportStudentOut
    session: ReportSessionOut
    evaluation: ReportEvaluationOut | None
    turns: list[ReportTurnOut]
    scores: list[ReportScoreOut]
    concept_results: list[ReportConceptResultOut]
    flags: list[ReportFlagOut]


class SafetyActionIn(Body):
    action: Literal["resume", "end"]
    note: str | None = Field(default=None, max_length=1000)


class SafetyActionOut(BaseModel):
    session_id: UUID
    status: str
    acted_at: datetime
