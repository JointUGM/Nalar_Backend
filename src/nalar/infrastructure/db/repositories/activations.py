import hashlib
import json
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from nalar.application.features.onboarding.messages import invitation_message
from nalar.application.ports.activations import PendingInvitation
from nalar.application.ports.queue import DEFAULT_QUEUE
from nalar.infrastructure.db.pool import DbConnection
from nalar.infrastructure.queue.pgmq import PgmqSender

_INVITATION = """
    select n.id, a.id as activation_id, a.user_id, a.school_id, a.issued_by,
           a.recipient_email, n.payload
      from notifications n join account_activations a
        on n.dedupe_key = 'account-invitation:' || a.id::text
       and n.recipient_id = a.user_id and n.school_id = a.school_id
     where n.id = $1 and n.type = 'account_invitation' and n.status = 'pending'
       and n.payload->>'transport' = 'supabase_auth_v1'
"""


class PgActivationsRepo:
    def __init__(self, conn: DbConnection) -> None:
        self._conn = conn

    def _invitation(self, row: Any) -> PendingInvitation:
        payload = json.loads(row["payload"])
        return PendingInvitation(
            row["id"],
            row["activation_id"],
            row["user_id"],
            row["school_id"],
            row["issued_by"],
            row["recipient_email"],
            datetime.fromisoformat(payload["expires_at"]),
            int(payload.get("attempts", 0)),
        )

    async def get_invitation(self, notification_id: UUID) -> PendingInvitation | None:
        row = await self._conn.fetchrow(_INVITATION, notification_id)
        return self._invitation(row) if row and row["recipient_email"] else None

    async def eligible(self, invitation: PendingInvitation) -> bool:
        row = await self._conn.fetchval(
            """
            select p.id from profiles p join account_activations a on a.user_id = p.id
             where p.id = $1 and a.school_id = $2 and a.issued_by = $3
               and a.id = $5 and a.channel = 'email'
               and a.consumed_at is null and a.superseded_at is null
               and p.onboarding_required and p.has_real_email
               and lower(p.contact_email) = lower($4)
               and lower(a.recipient_email) = lower($4)
               and (exists (select 1 from school_memberships m
                             where m.school_id = $2 and m.user_id = p.id and m.status = 'active')
                    or exists (select 1 from parent_student_links l
                                join school_memberships m on m.user_id = l.student_id
                                 and m.school_id = l.school_id and m.role = 'student'
                                 and m.status = 'active'
                                join class_enrollments e on e.student_id = l.student_id
                                 and e.school_id = l.school_id and e.status = 'active'
                               where l.parent_id = p.id and l.school_id = $2))
             for update of p, a
            """,
            invitation.user_id,
            invitation.school_id,
            invitation.issuer_id,
            invitation.email,
            invitation.activation_id,
        )
        return row is not None

    async def _recover(self, now: datetime) -> None:
        await self._conn.execute(
            """
            update notifications set status = 'failed',
                   payload = (payload - 'token' - 'lease_until' - 'dispatch_until') ||
                       jsonb_build_object('outcome', 'unknown', 'category', 'acceptance_unknown')
             where type = 'account_invitation' and status = 'pending'
               and payload->>'transport' = 'supabase_auth_v1'
               and payload->>'phase' = 'submitting'
               and (payload->>'lease_until')::timestamptz <= $1
            """,
            now,
        )
        await self._conn.execute(
            """
            update notifications set status = 'failed',
                   payload = (payload - 'token' - 'lease_until' - 'dispatch_until') ||
                       jsonb_build_object('outcome', 'skipped', 'category', 'expired')
             where type = 'account_invitation' and status = 'pending'
               and payload->>'transport' = 'supabase_auth_v1'
               and payload->>'phase' = 'prepared'
               and (payload->>'expires_at')::timestamptz <= $1
               and (payload->>'lease_until' is null
                    or (payload->>'lease_until')::timestamptz <= $1)
            """,
            now,
        )

    async def reserve_due(self, now: datetime, dispatch_until: datetime, limit: int) -> list[UUID]:
        await self._recover(now)
        rows = await self._conn.fetch(
            """
            with due as (
                select id from notifications
                 where type = 'account_invitation' and status = 'pending'
                   and payload->>'transport' = 'supabase_auth_v1'
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
        sender_key: str,
        now: datetime,
        invitation: PendingInvitation,
        hourly_limit: int,
        daily_limit: int,
        sender_spacing: timedelta,
    ) -> datetime | None:
        rows = await self._conn.fetch(
            "select id, status::text, payload from notifications"
            " where type = 'account_invitation' and payload->>'sender_key' = $1",
            sender_key,
        )
        events: list[datetime] = []
        retry_at = now
        for row in rows:
            payload = json.loads(row["payload"])
            if payload.get("sender_suspended"):
                return invitation.queue_expires_at
            if hold := payload.get("sender_hold_until"):
                retry_at = max(retry_at, datetime.fromisoformat(hold))
            if (
                row["id"] != invitation.id
                and row["status"] == "pending"
                and payload.get("lease_until")
            ):
                retry_at = max(retry_at, datetime.fromisoformat(payload["lease_until"]))
            events.extend(datetime.fromisoformat(t) for t in payload.get("submission_times", []))
        events = sorted(t for t in events if t > now - timedelta(days=1))
        hourly = [t for t in events if t > now - timedelta(hours=1)]
        if len(hourly) >= hourly_limit:
            retry_at = max(retry_at, hourly[-hourly_limit] + timedelta(hours=1))
        if len(events) >= daily_limit:
            retry_at = max(retry_at, events[-daily_limit] + timedelta(days=1))
        if events:
            retry_at = max(retry_at, events[-1] + sender_spacing)
        return retry_at if retry_at > now else None

    async def claim(
        self,
        notification_id: UUID,
        token: UUID,
        now: datetime,
        lease_until: datetime,
        *,
        sender_key: str,
        hourly_limit: int,
        daily_limit: int,
        sender_spacing: timedelta,
        max_attempts: int,
    ) -> PendingInvitation | None:
        await self._sender_lock(sender_key)
        await self._recover(now)
        row = await self._conn.fetchrow(
            _INVITATION + " and n.payload->>'phase' = 'prepared' and n.scheduled_for <= $2"
            " and (n.payload->>'lease_until' is null"
            " or (n.payload->>'lease_until')::timestamptz <= $2) for update of n",
            notification_id,
            now,
        )
        if row is None or not row["recipient_email"]:
            return None
        invitation = self._invitation(row)
        payload = json.loads(row["payload"])
        if (
            invitation.attempts >= max_attempts
            or payload.get("sender_key", sender_key) != sender_key
        ):
            await self._conn.execute(
                "update notifications set status = 'failed', payload = payload ||"
                " jsonb_build_object('outcome', 'skipped', 'category', 'admission_exhausted')"
                " where id = $1",
                notification_id,
            )
            return None
        retry_at = await self._sender_retry_at(
            sender_key,
            now,
            invitation,
            hourly_limit,
            daily_limit,
            sender_spacing,
        )
        if retry_at:
            await self._conn.execute(
                "update notifications set scheduled_for = $2,"
                " payload = payload - 'dispatch_until' where id = $1",
                notification_id,
                retry_at,
            )
            return None
        await self._conn.execute(
            "update notifications set payload = payload || jsonb_build_object("
            " 'token', $2::uuid::text, 'lease_until', $3::timestamptz, 'sender_key', $4::text,"
            " 'attempts', coalesce((payload->>'attempts')::int, 0) + 1) where id = $1",
            notification_id,
            token,
            lease_until,
            sender_key,
        )
        return await self.get_invitation(notification_id)

    async def mark_submitting(
        self,
        notification_id: UUID,
        token: UUID,
        now: datetime,
        link_lifetime: timedelta,
        sender_key: str,
    ) -> bool:
        await self._sender_lock(sender_key)
        row = await self._conn.fetchrow(
            """
            update notifications set payload = payload || jsonb_build_object(
                'phase', 'submitting', 'submission_times',
                coalesce(payload->'submission_times', '[]'::jsonb) ||
                    jsonb_build_array($3::timestamptz))
             where id = $1 and type = 'account_invitation' and status = 'pending'
               and payload->>'token' = $2::uuid::text and payload->>'phase' = 'prepared'
               and payload->>'sender_key' = $4
               and (payload->>'lease_until')::timestamptz > $3
               and (payload->>'expires_at')::timestamptz > $3 returning payload
            """,
            notification_id,
            token,
            now,
            sender_key,
        )
        if row is None:
            return False
        payload = json.loads(row["payload"])
        await self._conn.execute(
            "update account_activations set expires_at = $2 where id = $1"
            " and consumed_at is null and superseded_at is null",
            UUID(payload["activation_id"]),
            now + link_lifetime,
        )
        return True

    async def finish(
        self,
        notification_id: UUID,
        token: UUID,
        status: str,
        now: datetime,
        *,
        outcome: str,
        category: str | None = None,
        retry_at: datetime | None = None,
        sender_hold_until: datetime | None = None,
        sender_suspended: bool = False,
    ) -> bool:
        metadata: dict[str, Any] = {"outcome": outcome, "category": category}
        if status == "pending":
            metadata["phase"] = "prepared"
        if sender_hold_until:
            metadata["sender_hold_until"] = sender_hold_until.isoformat()
        if sender_suspended:
            metadata["sender_suspended"] = True
        row = await self._conn.fetchval(
            "update notifications set status = $3::notification_status,"
            " sent_at = case when $3 = 'sent' then $4 else sent_at end,"
            " scheduled_for = coalesce($6::timestamptz, scheduled_for),"
            " payload = (payload - 'token' - 'lease_until' - 'dispatch_until') || $5::jsonb"
            " where id = $1 and type = 'account_invitation' and status = 'pending'"
            " and payload->>'token' = $2::uuid::text returning id",
            notification_id,
            token,
            status,
            now,
            json.dumps(metadata),
            retry_at,
        )
        return row is not None

    async def queue_initial(
        self,
        user_id: UUID,
        school_id: UUID,
        actor_id: UUID,
        now: datetime,
        expires_at: datetime,
    ) -> UUID | None:
        recipient = await self._conn.fetchval(
            """
            select p.contact_email from profiles p
             where p.id = $1 and p.onboarding_required and p.has_real_email
               and nullif(btrim(p.contact_email), '') is not null
               and exists (select 1 from school_memberships m
                            where m.school_id = $2 and m.user_id = $3
                              and m.role = 'school_admin' and m.status = 'active')
               and (exists (select 1 from school_memberships m
                             where m.school_id = $2 and m.user_id = p.id
                               and m.status = 'active')
                    or exists (select 1 from parent_student_links l
                                join school_memberships m on m.user_id = l.student_id
                                 and m.school_id = l.school_id and m.role = 'student'
                                 and m.status = 'active'
                                join class_enrollments e on e.student_id = l.student_id
                                 and e.school_id = l.school_id and e.status = 'active'
                               where l.parent_id = p.id and l.school_id = $2))
             for update of p
            """,
            user_id,
            school_id,
            actor_id,
        )
        if recipient is None or await self._conn.fetchval(
            "select exists(select 1 from account_activations where user_id = $1"
            " and channel = 'email')",
            user_id,
        ):
            return None
        activation_id = uuid4()
        await self._conn.execute(
            "insert into account_activations"
            " (id, user_id, school_id, channel, issued_by, recipient_email, expires_at, created_at)"
            " values ($1, $2, $3, 'email', $4, $5, $6, $7)",
            activation_id,
            user_id,
            school_id,
            actor_id,
            recipient,
            expires_at,
            now,
        )
        notification_id = uuid4()
        await self._conn.execute(
            "insert into notifications"
            " (id, recipient_id, school_id, type, payload, dedupe_key, scheduled_for, created_at)"
            " values ($1, $2, $3, 'account_invitation', $4::jsonb, $5, $6, $6)",
            notification_id,
            user_id,
            school_id,
            json.dumps(
                {
                    "activation_id": str(activation_id),
                    "transport": "supabase_auth_v1",
                    "phase": "prepared",
                    "expires_at": expires_at.isoformat(),
                }
            ),
            f"account-invitation:{activation_id}",
            now,
        )
        await PgmqSender(self._conn).send(DEFAULT_QUEUE, invitation_message(notification_id))
        return notification_id
