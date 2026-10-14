from datetime import datetime
from uuid import UUID

from nalar.application.ports.administration import AdminRow
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

_INBOX_SCOPE = """
    n.recipient_id = $1 and n.scheduled_for <= now()
    and exists(select 1 from schools sc where sc.id = n.school_id and sc.is_active)
    and (
      (n.type = 'account_invitation' and exists(
        select 1 from school_memberships m where m.user_id = $1
          and m.school_id = n.school_id and m.status = 'active'))
      or (n.type = 'wellbeing_alert' and exists(
        select 1 from publications p join mission_versions mv on mv.id = p.mission_version_id
          join missions mi on mi.id = mv.mission_id
          join knowledge_bases kb on kb.id = mi.knowledge_base_id
          join teaching_assignments ta on ta.class_id = p.class_id
            and ta.school_subject_id = kb.school_subject_id
          join school_memberships m on m.user_id = ta.teacher_id
            and m.school_id = ta.school_id and m.role = 'teacher' and m.status = 'active'
        where ta.teacher_id = $1 and p.school_id = n.school_id
          and p.cancelled_at is null and p.id::text = n.payload->>'publication_id'))
    )
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

    async def inbox(
        self, actor_id: UUID, limit: int, after: tuple[datetime, UUID] | None
    ) -> list[AdminRow]:
        rows = await self._conn.fetch(
            "select n.id, n.type::text as type, n.created_at, n.read_at from notifications n where "
            + _INBOX_SCOPE
            + " and ($3::timestamptz is null or (n.created_at,n.id) < ($3,$4::uuid))"
            " order by n.created_at desc, n.id desc limit $2",
            actor_id,
            limit,
            after[0] if after else None,
            after[1] if after else None,
        )
        return [dict(row) for row in rows]

    async def unread_count(self, actor_id: UUID) -> int:
        return int(
            await self._conn.fetchval(
                "select count(*)::int from notifications n where "
                + _INBOX_SCOPE
                + " and n.read_at is null",
                actor_id,
            )
        )

    async def read(self, actor_id: UUID, notification_id: UUID, now: datetime) -> bool:
        return (
            await self._conn.fetchval(
                "update notifications n set read_at = coalesce(read_at,$3) where n.id = $2 and "
                + _INBOX_SCOPE
                + " returning n.id",
                actor_id,
                notification_id,
                now,
            )
            is not None
        )
