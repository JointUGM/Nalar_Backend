from uuid import UUID

from nalar.application.cursor import decode_cursor, encode_cursor
from nalar.application.errors import NotFound
from nalar.application.ports.administration import AdminRow
from nalar.application.ports.clock import Clock
from nalar.application.ports.uow import UnitOfWork


class InboxHandler:
    def __init__(self, uow: UnitOfWork, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def _authorize(self, actor_id: UUID) -> None:
        me = await self._uow.identity.me(actor_id)
        if me is None or not (me.roles or me.is_parent or me.is_platform_admin):
            raise NotFound()

    async def page(self, actor_id: UUID, limit: int, cursor: str | None) -> AdminRow:
        async with self._uow:
            await self._authorize(actor_id)
            after = decode_cursor(cursor) if cursor else None
            rows = await self._uow.notifications.inbox(actor_id, limit + 1, after)
            unread = await self._uow.notifications.unread_count(actor_id)
        return {
            "items": rows[:limit],
            "unread_count": unread,
            "next_cursor": encode_cursor(rows[limit - 1]["created_at"], rows[limit - 1]["id"])
            if len(rows) > limit
            else None,
        }

    async def read(self, actor_id: UUID, notification_id: UUID) -> None:
        async with self._uow:
            await self._authorize(actor_id)
            if not await self._uow.notifications.read(actor_id, notification_id, self._clock.now()):
                raise NotFound()
