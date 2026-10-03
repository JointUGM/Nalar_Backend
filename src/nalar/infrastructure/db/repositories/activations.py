import hashlib
import json
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from nalar.application.features.onboarding.messages import invitation_message
from nalar.application.ports.activations import (
    InvitationAdmission,
    InvitationPage,
    InvitationRecipient,
    InvitationState,
    InvitationStatus,
    PendingInvitation,
)
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

_RECIPIENT_SCOPE = """
    (exists (select 1 from school_memberships m
              where m.school_id = $1 and m.user_id = p.id and m.status = 'active')
     or exists (select 1 from parent_student_links l
                 join school_memberships m on m.user_id = l.student_id
                  and m.school_id = l.school_id and m.role = 'student' and m.status = 'active'
                 join class_enrollments e on e.student_id = l.student_id
                  and e.school_id = l.school_id and e.status = 'active'
                where l.parent_id = p.id and l.school_id = $1))
"""

_STATUS = (
    """
    with recipients as (
        select p.id as user_id, p.full_name,
               coalesce((select m.role::text from school_memberships m
                          where m.school_id = $1 and m.user_id = p.id and m.status = 'active'
                          order by case m.role when 'school_admin' then 0
                                               when 'teacher' then 1 else 2 end limit 1),
                        'parent') as role,
               n.id as notification_id,
               case
                 when a.consumed_at is not null then 'activated'
                 when not p.has_real_email or nullif(btrim(p.contact_email), '') is null
                   then 'requires_assistance'
                 when not p.onboarding_required and a.id is not null then 'active'
                 when a.id is null then 'not_requested'
                 when a.superseded_at is not null then 'superseded'
                 when n.status = 'pending' and n.payload->>'phase' = 'prepared' then
                   case when (n.payload->>'expires_at')::timestamptz <= $2
                        then 'expired' else 'pending' end
                 when a.expires_at <= $2 then 'expired'
                 when n.status = 'pending' then 'pending'
                 when n.status = 'sent' then 'sent'
                 else 'failed'
               end as state,
               case when n.payload->>'category' in (
                   'ineligible', 'expired', 'auth_identity_changed', 'eligibility_changed',
                   'acceptance_unknown', 'admission_exhausted', 'auth', 'rate_limited',
                   'connect', 'transport', 'provider_error', 'deadline', 'auth_lookup',
                   'superseded',
                   'proof_binding', 'activation_failed', 'already_active'
               ) then n.payload->>'category' else null end as reason,
               a.created_at, n.sent_at,
               case when n.status = 'pending' and n.payload->>'phase' = 'prepared'
                    then (n.payload->>'expires_at')::timestamptz else a.expires_at end as expires_at
          from profiles p
          left join lateral (
              select a.* from account_activations a
               where a.user_id = p.id and a.school_id = $1 and a.channel = 'email'
               order by a.created_at desc, a.id desc limit 1
          ) a on true
          left join notifications n on n.dedupe_key = 'account-invitation:' || a.id::text
           and n.school_id = $1 and n.recipient_id = p.id and n.type = 'account_invitation'
         where
"""
    + _RECIPIENT_SCOPE
    + ") "
)


class PgActivationsRepo:
    def __init__(self, conn: DbConnection) -> None:
        self._conn = conn

    async def bind_proof(self, notification_id: UUID, token: UUID) -> bool:
        return (
            await self._conn.fetchval(
                """
            update account_activations a set code_hash =
                   encode(sha256(convert_to(u.recovery_token, 'UTF8')), 'hex')
              from notifications n, auth.users u
             where n.id=$1 and n.payload->>'token'=$2::uuid::text and n.status='pending'
               and n.payload->>'phase'='submitting'
               and n.dedupe_key='account-invitation:' || a.id::text
               and u.id=a.user_id and lower(u.email)=lower(a.recipient_email)
               and nullif(u.recovery_token, '') is not null
               and a.code_hash is distinct from
                   encode(sha256(convert_to(u.recovery_token, 'UTF8')), 'hex')
               and a.consumed_at is null and a.superseded_at is null
            returning a.id
            """,
                notification_id,
                token,
            )
            is not None
        )

    async def claim_activation(
        self,
        activation_id: UUID,
        proof_digest: str,
        token: UUID,
        now: datetime,
        lease_until: datetime,
    ) -> PendingInvitation | None:
        row = await self._conn.fetchrow(
            "select n.id, a.id as activation_id, a.user_id, a.school_id, a.issued_by,"
            " a.recipient_email, n.payload from account_activations a join notifications n"
            " on n.dedupe_key='account-invitation:' || a.id::text"
            " and n.school_id=a.school_id and n.recipient_id=a.user_id"
            " where a.id=$1 and a.channel='email' and a.code_hash=$2 and a.expires_at>$3"
            " and a.consumed_at is null and a.superseded_at is null"
            " and n.type='account_invitation' and n.status='sent'"
            " and n.payload->>'transport'='supabase_auth_v1'",
            activation_id,
            proof_digest,
            now,
        )
        if row is None:
            return None
        invitation = self._invitation(row)
        if not await self._activation_eligible(invitation):
            return None
        claimed = await self._conn.fetchval(
            "update notifications n set payload=payload || jsonb_build_object("
            " 'activation_token',$2::uuid::text,'activation_until',$4::timestamptz,"
            " 'activation_phase','verifying') from account_activations a"
            " where n.id=$1 and n.status='sent' and a.id=$5 and a.code_hash=$6"
            " and a.expires_at>$3 and a.consumed_at is null and a.superseded_at is null"
            " and (n.payload->>'activation_until' is null"
            " or (n.payload->>'activation_until')::timestamptz<=$3) returning n.id",
            invitation.id,
            token,
            now,
            lease_until,
            activation_id,
            proof_digest,
        )
        return invitation if claimed else None

    async def _activation_eligible(self, invitation: PendingInvitation) -> bool:
        return bool(
            await self._conn.fetchval(
                "select exists(select 1 from school_memberships where user_id=$1 and school_id=$2"
                " and role='school_admin' and status='active')",
                invitation.issuer_id,
                invitation.school_id,
            )
        ) and await self.eligible(invitation)

    async def _activation_current(
        self,
        invitation: PendingInvitation,
        token: UUID,
        now: datetime,
    ) -> bool:
        return await self._activation_eligible(invitation) and bool(
            await self._conn.fetchval(
                "select exists(select 1 from notifications n join account_activations a"
                " on a.id=$4 where n.id=$1 and n.status='sent'"
                " and n.payload->>'activation_token'=$2::uuid::text"
                " and (n.payload->>'activation_until')::timestamptz>$3 and a.expires_at>$3"
                " and a.consumed_at is null and a.superseded_at is null)",
                invitation.id,
                token,
                now,
                invitation.activation_id,
            )
        )

    async def checkpoint_activation(
        self,
        invitation: PendingInvitation,
        token: UUID,
        now: datetime,
    ) -> bool:
        if not await self._activation_current(invitation, token, now):
            return False
        return (
            await self._conn.fetchval(
                "update notifications set payload=payload ||"
                ' \'{"activation_phase":"password_submitting"}\'::jsonb'
                " where id=$1 and payload->>'activation_token'=$2::uuid::text"
                " and payload->>'activation_phase'='verifying' returning id",
                invitation.id,
                token,
            )
            is not None
        )

    async def complete_activation(
        self,
        invitation: PendingInvitation,
        token: UUID,
        now: datetime,
    ) -> bool:
        if not await self._activation_current(invitation, token, now):
            return False
        if not await self._conn.fetchval(
            "select exists(select 1 from notifications where id=$1"
            " and payload->>'activation_phase'='password_submitting')",
            invitation.id,
        ):
            return False
        await self._conn.execute(
            "update account_activations set consumed_at=$2 where id=$1",
            invitation.activation_id,
            now,
        )
        await self._conn.execute(
            "update profiles set onboarding_required=false where id=$1", invitation.user_id
        )
        await self._conn.execute(
            "update notifications set payload=(payload-'activation_token'-'activation_until') ||"
            ' \'{"activation_phase":"completed"}\'::jsonb where id=$1',
            invitation.id,
        )
        return True

    async def abort_activation(self, notification_id: UUID, token: UUID, *, failed: bool) -> None:
        await self._conn.execute(
            "update notifications set status=case when $3 then 'failed'::notification_status"
            " else status end,"
            " payload=(payload-'activation_token'-'activation_until') || jsonb_build_object("
            " 'activation_phase','failed','category',"
            " case when $3 then 'activation_failed' else null end)"
            " where id=$1 and payload->>'activation_token'=$2::uuid::text",
            notification_id,
            token,
            failed,
        )

    async def reconcile_login(self, user_id: UUID, now: datetime) -> None:
        profile = await self._conn.fetchval(
            "select id from profiles where id=$1 and onboarding_required for update",
            user_id,
        )
        if profile is None:
            return
        await self._conn.execute(
            "update account_activations set consumed_at=$2 where user_id=$1"
            " and channel='email' and consumed_at is null and superseded_at is null",
            user_id,
            now,
        )
        await self._conn.execute(
            "update profiles set onboarding_required=false where id=$1", user_id
        )
        await self._conn.execute(
            "update notifications set status=case when status='pending'"
            " then 'failed'::notification_status else status end,"
            " payload=(payload-'token'-'lease_until'-'dispatch_until'"
            " -'activation_token'-'activation_until') ||"
            ' \'{"activation_phase":"completed","category":"already_active"}\'::jsonb'
            " where recipient_id=$1 and type='account_invitation'"
            " and payload->>'transport'='supabase_auth_v1'",
            user_id,
        )

    async def request_recipients(
        self, school_id: UUID, user_ids: list[UUID], *, lock: bool = False
    ) -> list[InvitationRecipient]:
        rows = await self._conn.fetch(
            "select p.id, p.contact_email, p.has_real_email, p.onboarding_required"
            " from profiles p where "
            + _RECIPIENT_SCOPE
            + " and p.id = any($2::uuid[]) order by p.id"
            + (" for update of p" if lock else ""),
            school_id,
            user_ids,
        )
        return [
            InvitationRecipient(
                row["id"], row["contact_email"], row["has_real_email"], row["onboarding_required"]
            )
            for row in rows
        ]

    async def enable_onboarding(self, user_id: UUID, email: str) -> bool:
        return (
            await self._conn.fetchval(
                "update profiles p set onboarding_required = true where p.id = $1"
                " and p.has_real_email and lower(p.contact_email) = lower($2)"
                " and not exists(select 1 from account_activations a where a.user_id=p.id"
                " and a.consumed_at is not null) returning id",
                user_id,
                email,
            )
            is not None
        )

    async def request_invitation(
        self,
        user_id: UUID,
        school_id: UUID,
        actor_id: UUID,
        now: datetime,
        expires_at: datetime,
        *,
        resend: bool,
        cooldown: timedelta,
    ) -> InvitationAdmission:
        row = await self._conn.fetchrow(
            "select a.id, a.school_id, a.expires_at, a.created_at, a.superseded_at,"
            " a.consumed_at, n.id as notification_id, n.status::text, n.sent_at, n.payload"
            " from account_activations a left join notifications n"
            " on n.dedupe_key='account-invitation:' || a.id::text and n.type='account_invitation'"
            " and n.recipient_id=a.user_id and n.school_id=a.school_id"
            " where a.user_id=$1 and a.channel='email'"
            " order by a.created_at desc, a.id desc limit 1",
            user_id,
        )
        if row:
            payload = json.loads(row["payload"]) if row["payload"] else {}
            own_notification = row["notification_id"] if row["school_id"] == school_id else None
            if row["consumed_at"] is not None:
                return InvitationAdmission(user_id, None, False, "already_active")
            if (
                payload.get("activation_until")
                and datetime.fromisoformat(payload["activation_until"]) > now
            ):
                return InvitationAdmission(user_id, None, False, "in_progress")
            live = row["superseded_at"] is None
            leased = (
                payload.get("lease_until") and datetime.fromisoformat(payload["lease_until"]) > now
            )
            if live and row["status"] == "pending" and leased:
                return InvitationAdmission(user_id, own_notification, False, "in_progress")
            if live and row["status"] == "pending" and payload.get("phase") == "submitting":
                await self._conn.execute(
                    "update notifications set status='failed',"
                    " payload=(payload - 'token' - 'lease_until' - 'dispatch_until') ||"
                    ' \'{"outcome":"unknown","category":"acceptance_unknown"}\'::jsonb'
                    " where id=$1 and status='pending'",
                    row["notification_id"],
                )
                retryable = True
            else:
                expiry = (
                    datetime.fromisoformat(payload["expires_at"])
                    if row["status"] == "pending" and payload.get("phase") == "prepared"
                    else row["expires_at"]
                )
                retryable = row["status"] == "failed" or expiry <= now or not live
            if live and not retryable:
                reason = "already_pending" if row["status"] == "pending" else "already_sent"
                return InvitationAdmission(
                    user_id, own_notification if row["status"] == "pending" else None, False, reason
                )
            if not resend:
                return InvitationAdmission(user_id, None, False, "resend_required")
            last_submission = max(
                [row["created_at"], row["sent_at"] or row["created_at"]]
                + [datetime.fromisoformat(value) for value in payload.get("submission_times", [])]
            )
            if last_submission + cooldown > now:
                return InvitationAdmission(user_id, None, False, "cooldown")
            await self._conn.execute(
                "update account_activations set superseded_at=$2 where user_id=$1"
                " and channel='email' and consumed_at is null and superseded_at is null",
                user_id,
                now,
            )
            if row["status"] == "pending":
                await self._conn.execute(
                    "update notifications set status='failed',"
                    " payload=(payload - 'token' - 'lease_until' - 'dispatch_until') ||"
                    ' \'{"outcome":"skipped","category":"superseded"}\'::jsonb'
                    " where id=$1 and status='pending'",
                    row["notification_id"],
                )
        recipient = await self._conn.fetchval(
            "select contact_email from profiles"
            " where id=$1 and onboarding_required and has_real_email",
            user_id,
        )
        if not recipient:
            return InvitationAdmission(user_id, None, False, "requires_assistance")
        notification_id = await self._create_invitation(
            user_id, school_id, actor_id, recipient, now, expires_at
        )
        return InvitationAdmission(user_id, notification_id, True, "resend" if row else "initial")

    async def list_invitations(
        self,
        school_id: UUID,
        now: datetime,
        cursor: UUID | None,
        limit: int,
        state: InvitationState | None = None,
    ) -> InvitationPage:
        counts_rows = await self._conn.fetch(
            _STATUS + "select state, count(*) as count from recipients group by state",
            school_id,
            now,
        )
        rows = await self._conn.fetch(
            _STATUS + "select * from recipients where ($3::uuid is null or user_id > $3)"
            " and ($5::text is null or state = $5)"
            " order by user_id limit $4",
            school_id,
            now,
            cursor,
            limit + 1,
            state,
        )
        items = [
            InvitationStatus(
                row["user_id"],
                row["notification_id"],
                cast(InvitationState, row["state"]),
                row["reason"],
                row["created_at"],
                row["sent_at"],
                row["expires_at"],
                row["full_name"],
                row["role"],
            )
            for row in rows[:limit]
        ]
        counts = {cast(InvitationState, row["state"]): int(row["count"]) for row in counts_rows}
        return InvitationPage(
            items,
            counts,
            counts.get(state, 0) if state else sum(counts.values()),
            items[-1].user_id if len(rows) > limit else None,
        )

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
            "update account_activations a set expires_at = $2, code_hash=("
            " select encode(sha256(convert_to(nullif(u.recovery_token,''),'UTF8')),'hex')"
            " from auth.users u where u.id=a.user_id) where a.id = $1"
            " and a.consumed_at is null and a.superseded_at is null",
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
        return await self._create_invitation(
            user_id, school_id, actor_id, recipient, now, expires_at
        )

    async def _create_invitation(
        self,
        user_id: UUID,
        school_id: UUID,
        actor_id: UUID,
        recipient: str,
        now: datetime,
        expires_at: datetime,
    ) -> UUID:
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
