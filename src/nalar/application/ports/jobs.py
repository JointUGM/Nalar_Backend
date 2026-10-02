from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID


@dataclass(frozen=True)
class Job:
    id: UUID
    school_id: UUID | None
    kind: str
    status: str
    entity_type: str
    entity_id: UUID
    requested_by: UUID | None
    attempts: int
    error_code: str | None
    updated_at: datetime
    result: Mapping[str, Any] | None = None


class JobStore(Protocol):
    async def create(
        self,
        *,
        kind: str,
        entity_type: str,
        entity_id: UUID,
        school_id: UUID | None,
        requested_by: UUID | None,
    ) -> UUID: ...

    async def get(self, job_id: UUID) -> Job | None: ...

    async def mark_running(self, job_id: UUID) -> bool:
        """D-S09-3: False when the job already succeeded or failed (a duplicate message)."""
        ...

    async def mark_succeeded(self, job_id: UUID) -> None: ...

    async def mark_failed(self, job_id: UUID, error_code: str, error_message: str) -> None: ...
