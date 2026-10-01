import json
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from nalar.infrastructure.db.pool import DbConnection


class PgAuditRepo:
    def __init__(self, conn: DbConnection) -> None:
        self._conn = conn

    async def session_action(
        self, session_id: UUID, actor_id: UUID, action: str, changes: Mapping[str, Any]
    ) -> None:
        await self._conn.execute(
            "insert into audit_logs (school_id, actor_id, action, entity_table, entity_id, changes)"
            " select s.school_id, $2, $3, 'sessions', s.id, $4::jsonb from sessions s"
            " where s.id = $1",
            session_id,
            actor_id,
            action,
            json.dumps(dict(changes)),
        )

    async def record(
        self,
        school_id: UUID | None,
        actor_id: UUID | None,
        action: str,
        entity_table: str,
        entity_id: UUID,
        changes: Mapping[str, Any],
    ) -> None:
        await self._conn.execute(
            "insert into audit_logs (school_id, actor_id, action, entity_table, entity_id, changes)"
            " values ($1, $2, $3, $4, $5, $6::jsonb)",
            school_id,
            actor_id,
            action,
            entity_table,
            entity_id,
            json.dumps(dict(changes)),
        )
