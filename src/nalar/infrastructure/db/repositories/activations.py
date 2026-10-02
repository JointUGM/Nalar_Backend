import json
from datetime import datetime
from uuid import UUID, uuid4

from nalar.application.features.onboarding.messages import invitation_message
from nalar.application.ports.queue import DEFAULT_QUEUE
from nalar.infrastructure.db.pool import DbConnection
from nalar.infrastructure.queue.pgmq import PgmqSender


class PgActivationsRepo:
    def __init__(self, conn: DbConnection) -> None:
        self._conn = conn

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
