import hashlib
import json
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from nalar.application.ports.administration import AdminRow
from nalar.application.ports.notifications import PendingDigest
from nalar.infrastructure.db.pool import DbConnection
from nalar.infrastructure.db.repositories.parents import VISIBLE_SQL

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


_INBOX_SCOPE = (
    """
    n.recipient_id = $1 and n.scheduled_for <= now()
    and exists(select 1 from schools sc where sc.id = n.school_id and sc.is_active)
    and (
      (n.type = 'account_invitation' and (
              exists(select 1 from school_memberships m where m.user_id = $1 and m.school_id =
      n.school_id and m.status = 'active')
              or exists(select 1 from parent_student_links l where l.parent_id = $1 and
      l.school_id = n.school_id and l.deactivated_at is null)))
      or (n.type in ('release_reminder', 'wellbeing_alert') and exists(
        select 1 from publications p join mission_versions mv on mv.id = p.mission_version_id
              join missions mi on mi.id = mv.mission_id join knowledge_bases kb on kb.id =
      mi.knowledge_base_id
              join teaching_assignments ta on ta.class_id = p.class_id and ta.school_subject_id =
      kb.school_subject_id
              join school_memberships m on m.user_id = ta.teacher_id and m.school_id =
      ta.school_id and m.role = 'teacher' and m.status = 'active'
              where ta.teacher_id = $1 and p.school_id = n.school_id and p.cancelled_at is null
      and p.id::text = n.payload->>'publication_id'))
      or (n.type = 'parent_periodic_summary' and exists(
        select 1 from parent_student_links l
        join lateral ("""
    + VISIBLE_SQL.replace("$1", "l.student_id")
    + """) v on true
        join lateral jsonb_array_elements(case when jsonb_typeof(n.payload->'items') = 'array'
             then n.payload->'items' else '[]'::jsonb end) item on true
        where l.parent_id = $1 and l.school_id = n.school_id and l.deactivated_at is null
                and v.publication_id::text = item->>'publication_id' and l.student_id::text =
      item->>'student_id'))
    )
"""
)


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
        expires_at: datetime,
    ) -> None:
        payload = {
            "items": items,
            "transport": "gmail_smtp_v1",
            "phase": "prepared",
            "expires_at": expires_at.isoformat(),
        }
        await self._conn.execute(
            "insert into notifications"
            " (recipient_id, school_id, type, payload, dedupe_key, scheduled_for)"
            " values ($1, $2, 'parent_periodic_summary', $3::jsonb, $4, $5)"
            " on conflict (dedupe_key) do update"
            " set payload = notifications.payload || excluded.payload"
            " where notifications.status = 'pending'"
            " and notifications.payload->>'transport' is distinct from 'gmail_smtp_v1'"
            " and coalesce((notifications.payload->>'attempts')::int, 0) = 0",
            recipient_id,
            school_id,
            json.dumps(payload),
            dedupe_key,
            now,
        )

    async def _recover(self, now: datetime) -> None:
        await self._conn.execute(
            """
            update notifications set status = 'failed',
                   payload = (payload - 'token' - 'lease_until' - 'dispatch_until') ||
                     jsonb_build_object('outcome', 'unknown', 'category', 'acceptance_unknown')
             where type = 'parent_periodic_summary' and status = 'pending'
               and ((payload->>'phase' = 'submitting'
                     and (payload->>'lease_until')::timestamptz <= $1)
                 or (payload->>'transport' is distinct from 'gmail_smtp_v1'
                     and coalesce((payload->>'attempts')::int, 0) > 0))
            """,
            now,
        )
        await self._conn.execute(
            """
            update notifications set status = 'failed',
                   payload = (payload - 'token' - 'lease_until' - 'dispatch_until') ||
                     jsonb_build_object('outcome', 'skipped', 'category', 'expired')
             where type = 'parent_periodic_summary' and status = 'pending'
               and payload->>'phase' = 'prepared'
               and (payload->>'expires_at')::timestamptz <= $1
               and (payload->>'lease_until' is null
                    or (payload->>'lease_until')::timestamptz <= $1)
            """,
            now,
        )

    @staticmethod
    def _digest(row: Any) -> PendingDigest:
        return PendingDigest(
            row["id"], row["recipient_id"], row["dedupe_key"], json.loads(row["payload"])
        )

    async def pending_digests(self, now: datetime) -> list[PendingDigest]:
        await self._recover(now)
        rows = await self._conn.fetch(
            "select id, recipient_id, dedupe_key, payload from notifications"
            " where type = 'parent_periodic_summary' and status = 'pending'"
            " and scheduled_for <= $1 and payload->>'phase' = 'prepared'"
            " and (payload->>'lease_until' is null or (payload->>'lease_until')::timestamptz <= $1)"
            " order by scheduled_for, id",
            now,
        )
        return [self._digest(row) for row in rows]

    async def get_digest(self, digest_id: UUID) -> PendingDigest | None:
        row = await self._conn.fetchrow(
            "select id, recipient_id, dedupe_key, payload from notifications"
            " where id = $1 and type = 'parent_periodic_summary' and status = 'pending'",
            digest_id,
        )
        return self._digest(row) if row else None

    async def reserve_due_digests(
        self, now: datetime, dispatch_until: datetime, limit: int
    ) -> list[UUID]:
        await self._recover(now)
        rows = await self._conn.fetch(
            """
            with due as (
                select id from notifications
                 where type = 'parent_periodic_summary' and status = 'pending'
                   and payload->>'phase' = 'prepared' and scheduled_for <= $1
                   and (payload->>'lease_until' is null
                        or (payload->>'lease_until')::timestamptz <= $1)
                   and (payload->>'dispatch_until' is null
                        or (payload->>'dispatch_until')::timestamptz <= $1)
                 order by scheduled_for, id limit $3 for update skip locked
            )
            update notifications n set payload = n.payload ||
                   jsonb_build_object('dispatch_until', $2::timestamptz)
              from due where n.id = due.id returning n.id
            """,
            now,
            dispatch_until,
            limit,
        )
        return [row["id"] for row in rows]

    async def _sender_lock(self, sender_key: str) -> None:
        key = int.from_bytes(hashlib.sha256(sender_key.encode()).digest()[:8], signed=True)
        await self._conn.execute("select pg_advisory_xact_lock($1)", key)

    async def _sender_retry_at(
        self,
        digest_id: UUID,
        sender_key: str,
        now: datetime,
        daily_limit: int,
        sender_spacing: timedelta,
    ) -> datetime | None:
        rows = await self._conn.fetch(
            "select id, status::text, payload from notifications"
            " where type = 'parent_periodic_summary' and payload->>'sender_key' = $1",
            sender_key,
        )
        events: list[datetime] = []
        retry_at = now
        for row in rows:
            payload = json.loads(row["payload"])
            if payload.get("sender_suspended"):
                return now + timedelta(days=1)
            hold = payload.get("sender_hold_until")
            if hold:
                retry_at = max(retry_at, datetime.fromisoformat(hold))
            if row["id"] != digest_id and row["status"] == "pending" and payload.get("lease_until"):
                retry_at = max(retry_at, datetime.fromisoformat(payload["lease_until"]))
            events.extend(datetime.fromisoformat(t) for t in payload.get("submission_times", []))
        events = sorted(t for t in events if t > now - timedelta(days=1))
        if len(events) >= daily_limit:
            retry_at = max(retry_at, events[-daily_limit] + timedelta(days=1))
        if events:
            retry_at = max(retry_at, events[-1] + sender_spacing)
        return retry_at if retry_at > now else None

    async def claim_digest(
        self,
        digest_id: UUID,
        now: datetime,
        lease_until: datetime,
        token: UUID,
        delivery: dict[str, Any],
        *,
        sender_key: str,
        daily_limit: int,
        sender_spacing: timedelta,
    ) -> PendingDigest | None:
        await self._sender_lock(sender_key)
        await self._recover(now)
        row = await self._conn.fetchrow(
            "select id, payload from notifications where id = $1"
            " and type = 'parent_periodic_summary' and status = 'pending'"
            " and payload->>'phase' = 'prepared' and scheduled_for <= $2"
            " and (payload->>'lease_until' is null or (payload->>'lease_until')::timestamptz <= $2)"
            " for update",
            digest_id,
            now,
        )
        if row is None:
            return None
        payload = json.loads(row["payload"])
        if payload.get("sender_key", sender_key) != sender_key:
            await self._conn.execute(
                "update notifications set status = 'failed', payload = payload ||"
                " jsonb_build_object('outcome', 'skipped', 'category', 'sender_changed')"
                " where id = $1",
                digest_id,
            )
            return None
        retry_at = await self._sender_retry_at(
            digest_id, sender_key, now, daily_limit, sender_spacing
        )
        if retry_at:
            await self._conn.execute(
                "update notifications set scheduled_for = $2, payload = payload - 'dispatch_until'"
                " where id = $1",
                digest_id,
                retry_at,
            )
            return None
        row = await self._conn.fetchrow(
            "update notifications set payload = payload || jsonb_build_object("
            " 'token', $4::uuid::text, 'lease_until', $3::timestamptz,"
            " 'sender_key', $6::text,"
            " 'prepared_at', coalesce(payload->>'prepared_at', $2::timestamptz::text),"
            " 'attempts', coalesce((payload->>'attempts')::int, 0) + 1,"
            " 'delivery', coalesce(payload->'delivery', $5::jsonb))"
            " where id = $1 returning id, recipient_id, dedupe_key, payload",
            digest_id,
            now,
            lease_until,
            token,
            json.dumps(delivery),
            sender_key,
        )
        return self._digest(row) if row else None

    async def mark_digest_submitting(
        self, digest_id: UUID, token: UUID, now: datetime, sender_key: str
    ) -> bool:
        await self._sender_lock(sender_key)
        row = await self._conn.fetchrow(
            """
            update notifications set payload = payload || jsonb_build_object(
                'phase', 'submitting', 'submission_times',
                coalesce(payload->'submission_times', '[]'::jsonb) ||
                    jsonb_build_array($3::timestamptz))
             where id = $1 and type = 'parent_periodic_summary' and status = 'pending'
               and payload->>'token' = $2::uuid::text and payload->>'phase' = 'prepared'
               and payload->>'sender_key' = $4
               and (payload->>'lease_until')::timestamptz > $3
               and (payload->>'expires_at')::timestamptz > $3
             returning id
            """,
            digest_id,
            token,
            now,
            sender_key,
        )
        return row is not None

    async def finish_digest(
        self,
        digest_id: UUID,
        token: UUID,
        status: str,
        now: datetime,
        *,
        outcome: Literal["accepted", "rejected", "unknown", "skipped"] = "skipped",
        category: str | None = None,
        retry_at: datetime | None = None,
        sender_hold_until: datetime | None = None,
        sender_suspended: bool = False,
        smtp_code: int | None = None,
        enhanced_code: str | None = None,
    ) -> bool:
        metadata: dict[str, Any] = {
            "outcome": outcome,
            "category": category,
            "smtp_code": smtp_code,
            "enhanced_code": enhanced_code,
        }
        if status == "pending":
            metadata["phase"] = "prepared"
        if sender_hold_until:
            metadata["sender_hold_until"] = sender_hold_until.isoformat()
        if sender_suspended:
            metadata["sender_suspended"] = True
        row = await self._conn.fetchrow(
            "update notifications set status = $3::notification_status,"
            " sent_at = case when $3 = 'sent' then $4 else sent_at end,"
            " scheduled_for = coalesce($6::timestamptz, scheduled_for),"
            " payload = (payload - 'lease_until' - 'token' - 'dispatch_until') || $5::jsonb"
            " where id = $1 and type = 'parent_periodic_summary' and status = 'pending'"
            " and payload->>'token' = $2::uuid::text returning id",
            digest_id,
            token,
            status,
            now,
            json.dumps(metadata),
            retry_at,
        )
        return row is not None

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
