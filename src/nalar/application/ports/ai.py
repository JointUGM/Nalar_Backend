from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from nalar.application.ports.ai_contract import (
    AlignCpIn,
    AlignCpOut,
    ChunkSectionOut,
    ClassInsightIn,
    ClassInsightOut,
    CpExcerptDraft,
    DedupeIn,
    DedupeOut,
    DetectSectionsOut,
    EmbedIn,
    EmbedOut,
    EvaluateIn,
    EvaluateOut,
    ExtractConceptsIn,
    ExtractConceptsOut,
    ExtractCurriculumIn,
    GenerateMisconceptionsIn,
    GenerateMisconceptionsOut,
    GenerateMissionIn,
    GenerateMissionOut,
    InvocationOut,
    NextTurnIn,
    NextTurnOut,
    ParentSummaryIn,
    ParentSummaryOut,
    SelectTargetsIn,
    SelectTargetsOut,
    WarmIn,
    WarmOut,
)


@dataclass(frozen=True)
class ChunkSpec:
    page_start: int
    page_end: int
    title: str
    next_title: str | None


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
    async def select_targets(
        self, body: SelectTargetsIn, request_id: str
    ) -> AiResult[SelectTargetsOut]: ...

    async def generate_mission(
        self, body: GenerateMissionIn, request_id: str
    ) -> AiResult[GenerateMissionOut]: ...

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

    async def detect_sections(
        self, pdf: bytes, fallback_title: str, request_id: str
    ) -> AiResult[DetectSectionsOut]: ...

    async def chunk_section(
        self, pdf: bytes, spec: ChunkSpec, request_id: str
    ) -> AiResult[ChunkSectionOut]: ...

    async def extract_concepts(
        self, body: ExtractConceptsIn, request_id: str
    ) -> AiResult[ExtractConceptsOut]: ...

    async def dedupe_concepts(self, body: DedupeIn, request_id: str) -> AiResult[DedupeOut]: ...

    async def align_cp(self, body: AlignCpIn, request_id: str) -> AiResult[AlignCpOut]: ...

    async def extract_curriculum(
        self, body: ExtractCurriculumIn, request_id: str
    ) -> AiResult[CpExcerptDraft]: ...

    async def generate_misconceptions(
        self, body: GenerateMisconceptionsIn, request_id: str
    ) -> AiResult[GenerateMisconceptionsOut]: ...


class AiInvocationLog(Protocol):
    async def record(
        self, school_id: UUID | None, invocations: Sequence[InvocationOut]
    ) -> list[UUID]:
        """Persist every invocation, successful or failed; return the new row ids in order."""
        ...
