from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class JobOut(BaseModel):
    id: UUID
    kind: str
    status: str
    entity_type: str
    entity_id: UUID
    error_code: str | None
    updated_at: datetime
    generation_result: "MissionGenerationResultOut | None" = None


class MissionGenerationResultOut(BaseModel):
    version_id: UUID
    version_number: int
    ungrounded_concept_ids: list[UUID]
