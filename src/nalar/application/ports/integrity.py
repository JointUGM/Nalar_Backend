from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from nalar.domain.integrity import AnswerFacts, FlagDraft
from nalar.domain.telemetry import TurnMetrics


@dataclass(frozen=True)
class SessionIntegrityInput:
    school_id: UUID
    turns: tuple[tuple[UUID, int, str | None, bool], ...]
    """(turn_id, turn_index, answer_text, safety_paused) in turn order."""
    quality: dict[UUID, int]
    batches: tuple[tuple[UUID | None, Sequence[dict[str, Any]]], ...]


class IntegrityRepo(Protocol):
    async def session_input(self, session_id: UUID) -> SessionIntegrityInput | None:
        """None unless the session is terminal and has its evaluation row."""
        ...

    async def upsert_metrics(self, school_id: UUID, metrics: dict[UUID, TurnMetrics]) -> None: ...

    async def insert_flags(
        self, school_id: UUID, session_id: UUID, flags: Sequence[FlagDraft]
    ) -> int:
        """Skips any (session, flag_type, turn) that already has a flag; returns rows added."""
        ...

    async def publication_answers(self, publication_id: UUID) -> list[AnswerFacts]:
        """Answers of terminal, evaluated sessions, excluding safety-paused turns."""
        ...

    async def school_of_publication(self, publication_id: UUID) -> UUID | None: ...
