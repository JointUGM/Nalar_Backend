import json
from datetime import datetime, timedelta
from uuid import UUID

from nalar.application.ports.password_resets import (
    CredentialSnapshot,
    PasswordRecipient,
    PendingReset,
)
from nalar.infrastructure.db.pool import DbConnection
from nalar.infrastructure.db.repositories.auth_delivery import sender_lock, sender_retry_at

_RECIPIENT = """
    select p.id as user_id, u.email from profiles p join auth.users u on u.id=p.id
     where p.id=$1 and u.email is not null and not p.onboarding_required
       and (u.banned_until is null or u.banned_until <= now())
       and (p.is_platform_admin
        or exists(select 1 from school_memberships m join schools s on s.id=m.school_id
                   where m.user_id=p.id and m.status='active' and s.is_active)
        or exists(select 1 from parent_student_links l
                   join schools s on s.id=l.school_id and s.is_active
                   join school_memberships m on m.user_id=l.student_id and m.school_id=l.school_id
                    and m.role='student' and m.status='active'
                   join class_enrollments e on e.student_id=l.student_id
                    and e.school_id=l.school_id and e.status='active'
                  where l.deactivated_at is null and l.parent_id=p.id))
"""


async def credential_snapshot(conn: DbConnection, user_id: UUID) -> CredentialSnapshot:
    row = await conn.fetchrow(
        "select credential_revision, exists(select 1 from password_mutations m"
        " where m.user_id=p.id and m.phase not in ('completed','failed')) as blocked"
        " from profiles p where p.id=$1",
        user_id,
    )
    return (
        CredentialSnapshot(row["credential_revision"], row["blocked"])
        if row
        else CredentialSnapshot(0, False)
    )


async def login_revision(conn: DbConnection, email: str) -> int:
    return int(
        await conn.fetchval(
            "select p.credential_revision from profiles p join auth.users u on u.id=p.id"
            " where lower(u.email)=$1",
            email,
        )
        or 0
    )


class PgPasswordResetsRepo:
    def __init__(self, conn: DbConnection) -> None:
        self._conn = conn

    async def recipient(self, user_id: UUID, *, recovery: bool) -> PasswordRecipient | None:
        query = _RECIPIENT
        if recovery:
            query += (
                " and p.has_real_email and lower(p.contact_email)=lower(u.email) "
                "and u.email_confirmed_at is not null"
            )
        row = await self._conn.fetchrow(query, user_id)
        return PasswordRecipient(row["user_id"], row["email"]) if row else None

    async def request(
        self, email: str, now: datetime, expires_at: datetime, cooldown: timedelta
    ) -> UUID | None:
        user_id = await self._conn.fetchval(
            "select p.id from profiles p join auth.users u on u.id=p.id where lower(u.email)=$1"
            " for update of p",
            email,
        )
        if user_id is None or await self.recipient(user_id, recovery=True) is None:
            return None
        await self._recover(now)
        if await self._conn.fetchval(
            "select exists(select 1 from password_resets where user_id=$1"
            " and (status in ('pending','submitting','sent','verifying') or created_at>$2))"
            " or exists(select 1 from account_activations where user_id=$1 and channel='email'"
            " and consumed_at is null and superseded_at is null and expires_at>$3)",
            user_id,
            now - cooldown,
            now,
        ):
            return None
        reset_id: UUID | None = await self._conn.fetchval(
            "insert into "
            "password_resets(user_id,recipient_email,queue_expires_at,"
            "expires_at,created_at,scheduled_for)"
            " values($1,$2,$3,$3,$4,$4) returning id",
            user_id,
            email,
            expires_at,
            now,
        )
        return reset_id

    async def _recover(self, now: datetime) -> None:
        await self._conn.execute(
            "update password_resets set status='failed',payload=(payload-'token'-'lease_until') ||"
            " jsonb_build_object('category',case when status='submitting' then "
            "'acceptance_unknown' else 'expired' end)"
            " where (status='pending' and queue_expires_at<=$1)"
            " or (status='sent' and expires_at<=$1)"
            " or (status in ('submitting','verifying') and "
            "(payload->>'lease_until')::timestamptz<=$1)",
            now,
        )

    async def reserve_due(self, now: datetime, dispatch_until: datetime, limit: int) -> list[UUID]:
        await self._recover(now)
        rows = await self._conn.fetch(
            "with due as (select id from password_resets where "
            "status='pending' and scheduled_for<=$1"
            " and (payload->>'lease_until' is null or (payload->>'lease_until')::timestamptz<=$1)"
            " and (payload->>'dispatch_until' is null or "
            "(payload->>'dispatch_until')::timestamptz<=$1)"
            " order by scheduled_for,id limit $3 for update skip locked)"
            " update password_resets r set payload=payload || "
            "jsonb_build_object('dispatch_until',$2::timestamptz)"
            " from due where r.id=due.id returning r.id",
            now,
            dispatch_until,
            limit,
        )
        return [r["id"] for r in rows]

    async def claim_delivery(
        self,
        reset_id: UUID,
        token: UUID,
        now: datetime,
        lease_until: datetime,
        sender_key: str,
        hourly_limit: int,
        daily_limit: int,
        sender_spacing: timedelta,
        max_attempts: int,
    ) -> PendingReset | None:
        await sender_lock(self._conn, sender_key)
        await self._recover(now)
        row = await self._conn.fetchrow(
            "select * from password_resets where id=$1 and status='pending' and scheduled_for<=$2"
            " and (payload->>'lease_until' is null or "
            "(payload->>'lease_until')::timestamptz<=$2) for update",
            reset_id,
            now,
        )
        if row is None:
            return None
        payload = json.loads(row["payload"])
        attempts = int(payload.get("attempts", 0))
        if attempts >= max_attempts or payload.get("sender_key", sender_key) != sender_key:
            await self._conn.execute(
                "update password_resets set status='failed' where id=$1", reset_id
            )
            return None
        retry_at = await sender_retry_at(
            self._conn,
            sender_key,
            now,
            reset_id,
            row["queue_expires_at"],
            hourly_limit,
            daily_limit,
            sender_spacing,
        )
        if retry_at:
            await self._conn.execute(
                "update password_resets set "
                "scheduled_for=$2,payload=payload-'dispatch_until' where id=$1",
                reset_id,
                retry_at,
            )
            return None
        await self._conn.execute(
            "update password_resets set payload=payload || "
            "jsonb_build_object('token',$2::uuid::text,"
            " 'lease_until',$3::timestamptz,'sender_key',$4::text,'attempts',$5::int) where id=$1",
            reset_id,
            token,
            lease_until,
            sender_key,
            attempts + 1,
        )
        return PendingReset(
            reset_id, row["user_id"], row["recipient_email"], attempts + 1, row["queue_expires_at"]
        )

    async def mark_submitting(
        self,
        reset: PendingReset,
        token: UUID,
        now: datetime,
        link_lifetime: timedelta,
        sender_key: str,
    ) -> bool:
        await sender_lock(self._conn, sender_key)
        await self._conn.fetchval("select id from profiles where id=$1 for update", reset.user_id)
        recipient = await self.recipient(reset.user_id, recovery=True)
        if recipient is None or recipient.email.lower() != reset.email.lower():
            return False
        if await self._conn.fetchval(
            "select exists(select 1 from account_activations where user_id=$1 and channel='email'"
            " and consumed_at is null and superseded_at is null and expires_at>$2)",
            reset.user_id,
            now,
        ):
            return False
        return (
            await self._conn.fetchval(
                "update password_resets r set status='submitting',expires_at=$4,proof_digest=("
                " select "
                "encode(sha256(convert_to(nullif(u.recovery_token,''),'UTF8')),'hex"
                "') from auth.users u where u.id=r.user_id),"
                " payload=payload || "
                "jsonb_build_object('submission_times',coalesce(payload->'submissio"
                "n_times','[]'::jsonb)"
                " || jsonb_build_array($3::timestamptz)) where id=$1 and status='pending'"
                " and payload->>'token'=$2::uuid::text and "
                "(payload->>'lease_until')::timestamptz>$3 returning id",
                reset.id,
                token,
                now,
                now + link_lifetime,
            )
            is not None
        )

    async def finish_delivery(
        self,
        reset_id: UUID,
        token: UUID,
        now: datetime,
        *,
        accepted: bool,
        category: str | None = None,
        retry_at: datetime | None = None,
        suspended: bool = False,
    ) -> None:
        bound = False
        if accepted:
            bound = (
                await self._conn.fetchval(
                    "update password_resets r set "
                    "proof_digest=encode(sha256(convert_to(u.recovery_token,'UTF8')),'h"
                    "ex')"
                    " from auth.users u where r.id=$1 and r.status='submitting' and "
                    "r.payload->>'token'=$2::uuid::text"
                    " and (r.payload->>'lease_until')::timestamptz>$3 and u.id=r.user_id"
                    " and lower(u.email)=lower(r.recipient_email) and "
                    "nullif(u.recovery_token,'') is not null"
                    " and r.proof_digest is distinct from "
                    "encode(sha256(convert_to(u.recovery_token,'UTF8')),'hex') "
                    "returning r.id",
                    reset_id,
                    token,
                    now,
                )
                is not None
            )
        metadata: dict[str, object] = {
            "category": category if not accepted else None if bound else "proof_binding"
        }
        if category == "rate_limited" and retry_at:
            metadata["sender_hold_until"] = retry_at.isoformat()
        if suspended:
            metadata["sender_suspended"] = True
        await self._conn.execute(
            "update password_resets set status=$3,scheduled_for=coalesce($4,scheduled_for),"
            " payload=(payload-'token'-'lease_until'-'dispatch_until') || $5::jsonb"
            " where id=$1 and payload->>'token'=$2::uuid::text and status in "
            "('pending','submitting')",
            reset_id,
            token,
            "sent" if bound else "pending" if retry_at else "failed",
            retry_at,
            json.dumps(metadata),
        )

    async def claim_proof(
        self, reset_id: UUID, digest: str, token: UUID, now: datetime, lease_until: datetime
    ) -> PasswordRecipient | None:
        row = await self._conn.fetchrow(
            "select user_id,recipient_email from password_resets where id=$1", reset_id
        )
        if row is None:
            return None
        recipient = await self.recipient(row["user_id"], recovery=True)
        if recipient is None or recipient.email.lower() != row["recipient_email"].lower():
            return None
        claimed = await self._conn.fetchval(
            "update password_resets set status='verifying',payload=payload || jsonb_build_object("
            " 'token',$3::uuid::text,'lease_until',$5::timestamptz) where id=$1 and status='sent'"
            " and proof_digest=$2 and expires_at>$4 returning id",
            reset_id,
            digest,
            token,
            now,
            lease_until,
        )
        return recipient if claimed else None

    async def fail_proof(self, reset_id: UUID, token: UUID) -> None:
        await self._conn.execute(
            "update password_resets set status='failed',payload=payload-'token'-'lease_until'"
            " where id=$1 and status='verifying' and payload->>'token'=$2::uuid::text",
            reset_id,
            token,
        )

    async def begin_mutation(
        self,
        user_id: UUID,
        token: UUID,
        now: datetime,
        lease_until: datetime,
        reset_id: UUID | None = None,
    ) -> bool:
        await self._conn.fetchval("select id from profiles where id=$1 for update", user_id)
        recipient = await self.recipient(user_id, recovery=reset_id is not None)
        if recipient is None or (await credential_snapshot(self._conn, user_id)).blocked:
            return False
        if reset_id is not None and not await self._conn.fetchval(
            "select exists(select 1 from password_resets where id=$1 and "
            "user_id=$2 and status='verifying'"
            " and payload->>'token'=$3::uuid::text and expires_at>$4"
            " and lower(recipient_email)=lower($5) and (payload->>'lease_until')::timestamptz>$4)",
            reset_id,
            user_id,
            token,
            now,
            recipient.email,
        ):
            return False
        await self._conn.execute(
            "insert into "
            "password_mutations(id,user_id,reset_id,phase,prior_password_digest,"
            "created_at,lease_until)"
            " select "
            "$1,$2,$3,'prepared',encode(sha256(convert_to(coalesce(encrypted_pa"
            "ssword,''),'UTF8')),'hex'),$4,$5"
            " from auth.users where id=$2",
            token,
            user_id,
            reset_id,
            now,
            lease_until,
        )
        await self._conn.execute(
            "update profiles set credential_revision=credential_revision+1 where id=$1", user_id
        )
        return True

    async def mutation_phase(self, token: UUID, phase: str, now: datetime) -> bool:
        allowed = {
            "password_submitting": ["prepared"],
            "password_changed": ["password_submitting"],
            "unknown": ["password_submitting", "password_changed"],
            "completed": ["password_changed", "unknown"],
            "failed": ["prepared", "password_submitting"],
        }
        row = await self._conn.fetchrow(
            "update password_mutations set phase=$2,completed_at=case when $2 "
            "in ('completed','failed') then $3 else null end"
            " where id=$1 and phase=any($4::text[])"
            " and ($2 in ('unknown','failed','completed') or lease_until>$3) "
            "returning user_id,reset_id",
            token,
            phase,
            now,
            allowed[phase],
        )
        if row is None:
            return False
        if phase == "completed":
            await self._conn.execute(
                "update profiles set must_change_password=false where id=$1", row["user_id"]
            )
            if row["reset_id"]:
                await self._conn.execute(
                    "update password_resets set "
                    "status='consumed',consumed_at=$2,payload=payload-'token'-'lease_until'"
                    " where id=$1",
                    row["reset_id"],
                    now,
                )
        return True

    async def reconcile_candidate(self, user_id: UUID, now: datetime) -> UUID | None:
        await self._conn.fetchval("select id from profiles where id=$1 for update", user_id)
        await self._conn.execute(
            "update password_mutations set phase='failed',completed_at=$2 where user_id=$1"
            " and phase='prepared' and lease_until<=$2",
            user_id,
            now,
        )
        candidate: UUID | None = await self._conn.fetchval(
            "select m.id from password_mutations m join auth.users u on u.id=m.user_id"
            " where m.user_id=$1 and m.phase in "
            "('password_submitting','password_changed','unknown')"
            " and m.lease_until<=$2 and m.prior_password_digest is distinct from"
            " encode(sha256(convert_to(coalesce(u.encrypted_password,''),'UTF8')),'hex')",
            user_id,
            now,
        )
        return candidate
