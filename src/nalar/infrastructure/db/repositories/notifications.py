import json
from datetime import datetime
from typing import Any
from uuid import UUID

from nalar.application.ports.notifications import PendingDigest
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

    async def queue_digest(
        self,
        recipient_id: UUID,
        school_id: UUID,
        dedupe_key: str,
        items: list[dict[str, str]],
        now: datetime,
    ) -> None:
        await self._conn.execute(
            "insert into notifications"
            " (recipient_id, school_id, type, payload, dedupe_key, scheduled_for)"
            " values ($1, $2, 'parent_periodic_summary', $3::jsonb, $4, $5)"
            " on conflict (dedupe_key) do nothing",
            recipient_id,
            school_id,
            json.dumps({"items": items}),
            dedupe_key,
            now,
        )

    async def pending_digests(self, now: datetime) -> list[PendingDigest]:
        rows = await self._conn.fetch(
            "select id, recipient_id, dedupe_key, payload from notifications"
            " where type = 'parent_periodic_summary' and status = 'pending'"
            " and scheduled_for <= $1"
            " and (payload->>'lease_until' is null or (payload->>'lease_until')::timestamptz <= $1)"
            " order by scheduled_for, id",
            now,
        )
        return [
            PendingDigest(r["id"], r["recipient_id"], r["dedupe_key"], json.loads(r["payload"]))
            for r in rows
        ]

    async def claim_digest(
        self,
        digest_id: UUID,
        now: datetime,
        lease_until: datetime,
        token: UUID,
        delivery: dict[str, Any],
    ) -> PendingDigest | None:
        row = await self._conn.fetchrow(
            "update notifications set payload = payload || jsonb_build_object("
            " 'token', $4::uuid::text, 'lease_until', $3::timestamptz,"
            " 'attempted_at', coalesce(payload->>'attempted_at', $2::timestamptz::text),"
            " 'attempts', coalesce((payload->>'attempts')::int, 0) + 1,"
            " 'delivery', coalesce(payload->'delivery', $5::jsonb))"
            " where id = $1 and type = 'parent_periodic_summary' and status = 'pending'"
            " and (payload->>'lease_until' is null or (payload->>'lease_until')::timestamptz <= $2)"
            " returning id, recipient_id, dedupe_key, payload",
            digest_id,
            now,
            lease_until,
            token,
            json.dumps(delivery),
        )
        return (
            PendingDigest(
                row["id"], row["recipient_id"], row["dedupe_key"], json.loads(row["payload"])
            )
            if row
            else None
        )

    async def finish_digest(
        self,
        digest_id: UUID,
        token: UUID,
        status: str,
        now: datetime,
    ) -> None:
        await self._conn.execute(
            "update notifications set status = $3::notification_status,"
            " sent_at = case when $3 = 'sent' then $4 else sent_at end,"
            " payload = payload - 'lease_until' - 'token'"
            " where id = $1 and status = 'pending' and payload->>'token' = $2::uuid::text",
            digest_id,
            token,
            status,
            now,
        )

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
