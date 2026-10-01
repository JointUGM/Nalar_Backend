from uuid import UUID

from nalar.infrastructure.db.pool import DbConnection

# dedupe_key is unique across all notifications, so it carries the recipient too.
# The payload holds ids only, never the student's words; the teacher opens the report.
_WELLBEING = """
    insert into notifications (recipient_id, school_id, type, payload, dedupe_key)
    select r, $2, 'wellbeing_alert',
           jsonb_build_object('session_id', $3::uuid::text, 'publication_id', $4::uuid::text,
                              'turn_index', $5::int),
           'wellbeing:' || $3::uuid::text || ':' || $5::int::text || ':' || r::text
      from unnest($1::uuid[]) as r
    on conflict (dedupe_key) do nothing
    returning id
"""

_RELEASE_REMINDER = """
    insert into notifications
           (recipient_id, school_id, type, payload, status, sent_at, dedupe_key)
    select r, $2, 'release_reminder', jsonb_build_object('publication_id', $3::uuid::text),
           'sent', now(), 'release_reminder:' || $3::uuid::text || ':' || r::text
      from unnest($1::uuid[]) as r
    on conflict (dedupe_key) do nothing
    returning id
"""


class PgNotificationsRepo:
    def __init__(self, conn: DbConnection) -> None:
        self._conn = conn

    async def wellbeing_alert(
        self,
        recipient_ids: list[UUID],
        school_id: UUID,
        session_id: UUID,
        publication_id: UUID,
        turn_index: int,
    ) -> int:
        rows = await self._conn.fetch(
            _WELLBEING, recipient_ids, school_id, session_id, publication_id, turn_index
        )
        return len(rows)

    async def release_reminder(
        self, recipient_ids: list[UUID], school_id: UUID, publication_id: UUID
    ) -> int:
        rows = await self._conn.fetch(_RELEASE_REMINDER, recipient_ids, school_id, publication_id)
        return len(rows)
