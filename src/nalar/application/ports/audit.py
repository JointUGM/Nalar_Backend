from collections.abc import Mapping
from typing import Any, Protocol
from uuid import UUID


class AuditRepo(Protocol):
    async def session_action(
        self, session_id: UUID, actor_id: UUID, action: str, changes: Mapping[str, Any]
    ) -> None: ...

    async def record(
        self,
        school_id: UUID | None,
        actor_id: UUID | None,
        action: str,
        entity_table: str,
        entity_id: UUID,
        changes: Mapping[str, Any],
    ) -> None: ...
