from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field, NonNegativeInt

from nalar.presentation.api.schemas.common import Body


class JoinIn(Body):
    join_code: str = Field(min_length=1, max_length=12)


class WarmupChoiceOut(BaseModel):
    id: str
    text: str


class WarmupOut(BaseModel):
    prompt: str
    choices: list[WarmupChoiceOut]


class JoinOut(BaseModel):
    run_id: UUID
    participant_id: UUID
    publication_id: UUID
    mission_title: str
    run_status: Literal["lobby", "open"]
    session_id: UUID | None
    warmup: WarmupOut | None
    deadline_at: datetime | None


class WarmupChoiceIn(Body):
    choice_id: str = Field(min_length=1, max_length=20)


class WarmupSavedOut(BaseModel):
    choice_id: str
    submitted_at: datetime
    scored: Literal[False] = False


class StudentLobbyOut(BaseModel):
    run_status: str
    participant_status: str
    warmup_choice_id: str | None
    session_id: UUID | None
    started_at: datetime | None
    deadline_at: datetime | None


class PromptOut(BaseModel):
    kind: str
    text: str
    turn_index: int


class WindowSessionOut(BaseModel):
    session_id: UUID
    status: str
    started_at: datetime
    deadline_at: datetime
    prompt: PromptOut


class MissionCardOut(BaseModel):
    publication_id: UUID
    mission_title: str
    subject_name: str
    mode: str
    opens_at: datetime | None
    closes_at: datetime | None
    run_status: str
    attempt_status: str
    target_duration_minutes: int = 15
    max_duration_minutes: int


class StudentMissionsOut(BaseModel):
    upcoming: list[MissionCardOut]
    open: list[MissionCardOut]
    completed: list[MissionCardOut]


class AnswerIn(Body):
    turn_index: int = Field(ge=0, le=50)
    answer_text: str = Field(min_length=1, max_length=4000)
    client_submission_id: UUID


class AnswerAccepted(BaseModel):
    status: Literal["processing"] = "processing"
    next_prompt_url: str


class StateOut(BaseModel):
    status: str
    turn_index: int
    probe_number: int
    probe_total: int
    started_at: datetime
    deadline_at: datetime
    prompt: PromptOut | None
    safety_message: str | None
    reflection_ready: bool


class TypingValue(Body):
    duration_ms: NonNegativeInt
    chars: NonNegativeInt


class PasteEvent(Body):
    type: Literal["paste"]
    at: AwareDatetime
    value: NonNegativeInt


class HiddenEvent(Body):
    type: Literal["visibility_hidden"]
    at: AwareDatetime
    value: NonNegativeInt


class ConnectionEvent(Body):
    type: Literal["disconnect", "reconnect"]
    at: AwareDatetime


class TypingEvent(Body):
    type: Literal["typing"]
    at: AwareDatetime
    value: TypingValue


# NFR-S11: closed event shapes with integer values only, so no key content can get through.
TelemetryEvent = Annotated[
    PasteEvent | HiddenEvent | ConnectionEvent | TypingEvent, Field(discriminator="type")
]


class TelemetryIn(Body):
    client_seq: int = Field(ge=0, le=2**31 - 1)
    turn_index: int | None = Field(default=None, ge=0, le=50)
    client_sent_at: AwareDatetime | None = None
    events: list[TelemetryEvent] = Field(min_length=1, max_length=200)


class TelemetryAccepted(BaseModel):
    accepted_client_seq: int
