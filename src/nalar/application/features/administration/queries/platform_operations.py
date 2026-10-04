from datetime import datetime, timedelta
from uuid import UUID

from nalar.application.errors import InvalidInput, NotFound
from nalar.application.ports.administration import AdminRow
from nalar.application.ports.uow import UnitOfWork


class PlatformOperationsHandler:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def school(
        self, actor_id: UUID, school_id: UUID, fields: AdminRow | None = None
    ) -> AdminRow:
        async with self._uow:
            if not await self._uow.authz.is_platform_admin(actor_id):
                raise NotFound()
            if fields is not None:
                if not await self._uow.administration.lock_school(school_id):
                    raise NotFound()
                await self._uow.administration.edit_school(school_id, fields)
                await self._uow.audit.record(
                    school_id,
                    actor_id,
                    "school.edited",
                    "schools",
                    school_id,
                    {"fields": sorted(fields)},
                )
            row = await self._uow.administration.school_detail(school_id)
            if row is None:
                raise NotFound()
            return row

    async def usage(self, actor_id: UUID, start: datetime, end: datetime) -> list[AdminRow]:
        async with self._uow:
            if not await self._uow.authz.is_platform_admin(actor_id):
                raise NotFound()
            if not timedelta(0) < end - start <= timedelta(days=93):
                raise InvalidInput("INVALID_USAGE_RANGE")
            return await self._uow.administration.ai_usage(start, end)

    async def audit(
        self, actor_id: UUID, school_id: UUID | None, limit: int, cursor: int | None
    ) -> AdminRow:
        async with self._uow:
            allowed = (
                await self._uow.authz.is_platform_admin(actor_id)
                if school_id is None
                else await self._uow.authz.is_school_admin(actor_id, school_id)
            )
            if not allowed:
                raise NotFound()
            rows = await self._uow.administration.audit_page(school_id, limit + 1, cursor)
        return {
            "items": rows[:limit],
            "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
        }
