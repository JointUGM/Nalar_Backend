from uuid import UUID

from nalar.application.ports.jobs import Job
from nalar.infrastructure.db.pool import DbConnection


class PgJobStore:
    def __init__(self, conn: DbConnection) -> None:
        self._conn = conn

    async def create(
        self,
        *,
        kind: str,
        entity_type: str,
        entity_id: UUID,
        school_id: UUID | None,
        requested_by: UUID | None,
    ) -> UUID:
        job_id: UUID = await self._conn.fetchval(
            "insert into jobs (kind, entity_type, entity_id, school_id, requested_by)"
            " values ($1, $2, $3, $4, $5) returning id",
            kind,
            entity_type,
            entity_id,
            school_id,
            requested_by,
        )
        return job_id

    async def get(self, job_id: UUID) -> Job | None:
        row = await self._conn.fetchrow(
            "select id, school_id, kind, status::text as status, entity_type, entity_id,"
            " requested_by, attempts, error_code, updated_at from jobs where id = $1",
            job_id,
        )
        return Job(**dict(row)) if row else None

    async def mark_running(self, job_id: UUID) -> bool:
        return (
            await self._conn.fetchval(
                "update jobs set status = 'running', attempts = attempts + 1"
                " where id = $1 and status in ('queued', 'running') returning id",
                job_id,
            )
            is not None
        )

    async def mark_succeeded(self, job_id: UUID) -> None:
        await self._conn.execute(
            "update jobs set status = 'succeeded', error_code = null, error_message = null"
            " where id = $1",
            job_id,
        )

    async def mark_failed(self, job_id: UUID, error_code: str, error_message: str) -> None:
        await self._conn.execute(
            "update jobs set status = 'failed', error_code = $2, error_message = $3 where id = $1",
            job_id,
            error_code,
            error_message,
        )
