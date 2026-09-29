from collections.abc import Mapping
from typing import Any, Protocol
from uuid import UUID


class AuditRepo(Protocol):
    async def session_action(
        self, session_id: UUID, actor_id: UUID, action: str, changes: Mapping[str, Any]
    ) -> None: ...
