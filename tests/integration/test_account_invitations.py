import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import asyncpg
import pytest

from nalar.application.features.onboarding.commands.send_invitation import (
    InvitationTiming,
    SendAccountInvitationHandler,
)
from nalar.application.ports.activations import PendingInvitation
from nalar.application.ports.auth_admin import AuthAccount, AuthEmailError
from tests.integration.support.factories import World
from tests.integration.support.uow import uow_on
from tests.unit.application.fakes import FakeClock

TIMING = InvitationTiming(
    True,
    "https://nalar.test/activate",
    "test-auth-project",
    15,
    timedelta(seconds=60),
    timedelta(seconds=1080),
    timedelta(hours=1),
    3,
    (timedelta(minutes=30), timedelta(hours=2)),
    20,
    200,
    32,
    timedelta(seconds=1),
    timedelta(minutes=30),
)


class FakeAuth:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn
        self.calls: list[tuple[str, str]] = []
        self.error: AuthEmailError | None = None
        self.revoke_during_lookup: UUID | None = None
        self.delay_s: float = 0

    async def get_account(self, user_id: UUID) -> AuthAccount | None:
        if self.revoke_during_lookup:
            await self.conn.execute(
                "update school_memberships set status = 'inactive' where user_id = $1",
                self.revoke_during_lookup,
            )
        row = await self.conn.fetchrow("select id, email from auth.users where id = $1", user_id)
        return AuthAccount(row["id"], None, row["email"]) if row else None

    async def create_or_find(self, email: str, full_name: str) -> AuthAccount:
        raise NotImplementedError

    async def send_setup_email(self, email: str, redirect_to: str) -> None:
        self.calls.append((email, redirect_to))
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.error:
            raise self.error
        await self.conn.execute(
            "update auth.users set recovery_token=$2, recovery_sent_at=now() where email=$1",
            email,
            uuid4().hex,
        )


async def queue(
    conn: asyncpg.Connection,
    world: World,
    clock: FakeClock,
    user_id: UUID | None = None,
) -> UUID:
    user_id = user_id or world.teacher_id
    await conn.execute(
        "update profiles set onboarding_required = true, has_real_email = true where id = $1",
        user_id,
    )
    async with uow_on(conn) as uow:
        notification_id = await uow.activations.queue_initial(
            user_id,
            world.school_id,
            world.admin_id,
            clock.now(),
            clock.now() + timedelta(days=2),
        )
    assert notification_id is not None
    return notification_id


async def state(conn: asyncpg.Connection, notification_id: UUID) -> tuple[str, dict[str, Any]]:
    row = await conn.fetchrow(
        "select status::text, payload from notifications where id=$1", notification_id
    )
    assert row is not None
    return row["status"], json.loads(row["payload"])


async def test_accepted_invitation_is_not_resent_and_link_lifetime_starts_at_submission(
    conn: asyncpg.Connection,
    world: World,
) -> None:
    clock, auth = FakeClock(), FakeAuth(conn)
    notification_id = await queue(conn, world, clock)
    clock.advance(hours=5)
    handler = SendAccountInvitationHandler(uow_on(conn), clock, auth, TIMING)
    await handler.execute(notification_id)
    await handler.execute(notification_id)
    status, payload = await state(conn, notification_id)
    assert status == "sent" and payload["outcome"] == "accepted"
    assert len(auth.calls) == 1
    assert (
        auth.calls[0][1] == f"https://nalar.test/activate?activation_id={payload['activation_id']}"
    )
    assert await conn.fetchval(
        "select expires_at from account_activations where user_id=$1", world.teacher_id
    ) == clock.now() + timedelta(hours=1)
    assert auth.calls[0][0] not in json.dumps(payload)


async def test_accepted_response_without_new_auth_proof_fails_closed(
    conn: asyncpg.Connection, world: World
) -> None:
    class NoProof(FakeAuth):
        async def send_setup_email(self, email: str, redirect_to: str) -> None:
            self.calls.append((email, redirect_to))

    clock, auth = FakeClock(), NoProof(conn)
    await conn.execute(
        "update auth.users set recovery_token='old-proof' where id=$1", world.teacher_id
    )
    notification_id = await queue(conn, world, clock)
    await SendAccountInvitationHandler(uow_on(conn), clock, auth, TIMING).execute(notification_id)
    status, payload = await state(conn, notification_id)
    assert status == "failed" and payload["category"] == "proof_binding"
    assert len(auth.calls) == 1


@pytest.mark.parametrize(
    "change", ["issuer", "membership", "profile_email", "auth_email", "generation"]
)
async def test_changed_eligibility_blocks_submission(
    conn: asyncpg.Connection,
    world: World,
    change: str,
) -> None:
    clock, auth = FakeClock(), FakeAuth(conn)
    notification_id = await queue(conn, world, clock)
    if change in ("issuer", "membership"):
        user_id = world.admin_id if change == "issuer" else world.teacher_id
        await conn.execute(
            "update school_memberships set status='inactive' where user_id=$1", user_id
        )
    elif change == "profile_email":
        await conn.execute(
            "update profiles set contact_email='changed@nalar.test' where id=$1", world.teacher_id
        )
    elif change == "auth_email":
        await conn.execute(
            "update auth.users set email='changed@nalar.test' where id=$1", world.teacher_id
        )
    else:
        await conn.execute(
            "update account_activations set superseded_at=now() where user_id=$1", world.teacher_id
        )
    await SendAccountInvitationHandler(uow_on(conn), clock, auth, TIMING).execute(notification_id)
    assert auth.calls == []
    assert (await state(conn, notification_id))[1]["outcome"] == "skipped"


async def test_revocation_during_auth_lookup_is_rechecked_before_submission(
    conn: asyncpg.Connection,
    world: World,
) -> None:
    clock, auth = FakeClock(), FakeAuth(conn)
    notification_id = await queue(conn, world, clock)
    auth.revoke_during_lookup = world.admin_id
    await SendAccountInvitationHandler(uow_on(conn), clock, auth, TIMING).execute(notification_id)
    assert auth.calls == []
    assert (await state(conn, notification_id))[1]["category"] == "eligibility_changed"


@pytest.mark.parametrize("window", ["hour", "day"])
async def test_unknown_acceptance_is_terminal_and_still_counts_against_sender_budget(
    conn: asyncpg.Connection,
    world: World,
    window: str,
) -> None:
    clock, auth = FakeClock(), FakeAuth(conn)
    first = await queue(conn, world, clock)
    second = await queue(conn, world, clock, world.student_id)
    auth.error = AuthEmailError("transport", acceptance_unknown=True)
    timing = replace(TIMING, hourly_limit=1) if window == "hour" else replace(TIMING, daily_limit=1)
    handler = SendAccountInvitationHandler(uow_on(conn), clock, auth, timing)
    await handler.execute(first)
    await handler.execute(first)
    await handler.execute(second)
    assert len(auth.calls) == 1
    assert (await state(conn, first))[1]["outcome"] == "unknown"
    assert (await state(conn, second))[0] == "pending"
    assert (await state(conn, second))[1].get("attempts", 0) == 0
    clock.advance(hours=1 if window == "hour" else 24)
    auth.error = None
    await handler.execute(second)
    assert len(auth.calls) == 2
    assert (await state(conn, second))[0] == "sent"


async def test_rate_rejection_honors_retry_after_and_reuses_the_generation(
    conn: asyncpg.Connection,
    world: World,
) -> None:
    clock, auth = FakeClock(), FakeAuth(conn)
    notification_id = await queue(conn, world, clock)
    auth.error = AuthEmailError("rate_limited", retryable=True, retry_after_s=3600)
    handler = SendAccountInvitationHandler(uow_on(conn), clock, auth, TIMING)
    await handler.execute(notification_id)
    clock.advance(minutes=30)
    await handler.execute(notification_id)
    assert len(auth.calls) == 1
    clock.advance(minutes=30)
    auth.error = None
    await handler.execute(notification_id)
    assert len(auth.calls) == 2
    assert auth.calls[0] == auth.calls[1]
    assert (await state(conn, notification_id))[1]["attempts"] == 2


async def test_crash_after_submission_checkpoint_is_quarantined_instead_of_retried(
    conn: asyncpg.Connection,
    world: World,
) -> None:
    clock = FakeClock()
    notification_id, token = await queue(conn, world, clock), uuid4()
    async with uow_on(conn) as uow:
        claimed = await uow.activations.claim(
            notification_id,
            token,
            clock.now(),
            clock.now() + TIMING.lease,
            sender_key=TIMING.sender_key,
            hourly_limit=20,
            daily_limit=200,
            sender_spacing=TIMING.sender_spacing,
            max_attempts=3,
        )
        assert claimed is not None
        assert (
            await uow.activations.claim(
                notification_id,
                uuid4(),
                clock.now(),
                clock.now() + TIMING.lease,
                sender_key=TIMING.sender_key,
                hourly_limit=20,
                daily_limit=200,
                sender_spacing=TIMING.sender_spacing,
                max_attempts=3,
            )
            is None
        )
        assert await uow.activations.mark_submitting(
            notification_id,
            token,
            clock.now(),
            TIMING.link_lifetime,
            TIMING.sender_key,
        )
    clock.advance(seconds=61)
    auth = FakeAuth(conn)
    handler = SendAccountInvitationHandler(uow_on(conn), clock, auth, TIMING)
    assert await handler.dispatch_due() == 0
    await handler.execute(notification_id)
    assert auth.calls == []
    assert (await state(conn, notification_id))[1]["outcome"] == "unknown"


async def test_auth_failure_pauses_other_invitations_and_dispatch_is_reserved_once(
    conn: asyncpg.Connection,
    world: World,
) -> None:
    clock, auth = FakeClock(), FakeAuth(conn)
    first = await queue(conn, world, clock)
    second = await queue(conn, world, clock, world.student_id)
    handler = SendAccountInvitationHandler(uow_on(conn), clock, auth, TIMING)
    assert await handler.dispatch_due() == 2
    assert await handler.dispatch_due() == 0
    auth.error = AuthEmailError("auth")
    await handler.execute(first)
    clock.advance(minutes=31)
    await handler.execute(second)
    assert len(auth.calls) == 1
    assert (await state(conn, first))[1]["sender_suspended"]


async def test_disabled_delivery_never_calls_auth_and_expired_queue_is_recovered(
    conn: asyncpg.Connection,
    world: World,
) -> None:
    clock, auth = FakeClock(), FakeAuth(conn)
    notification_id = await queue(conn, world, clock)
    handler = SendAccountInvitationHandler(
        uow_on(conn), clock, auth, replace(TIMING, enabled=False)
    )
    await handler.execute(notification_id)
    clock.advance(days=3)
    assert await handler.dispatch_due() == 0
    assert auth.calls == []
    assert (await state(conn, notification_id))[1]["category"] == "expired"


async def test_pre_submission_lease_can_be_reclaimed_after_process_death(
    conn: asyncpg.Connection,
    world: World,
) -> None:
    clock, auth = FakeClock(), FakeAuth(conn)
    notification_id = await queue(conn, world, clock)
    async with uow_on(conn) as uow:
        assert await uow.activations.claim(
            notification_id,
            uuid4(),
            clock.now(),
            clock.now() + TIMING.lease,
            sender_key=TIMING.sender_key,
            hourly_limit=20,
            daily_limit=200,
            sender_spacing=TIMING.sender_spacing,
            max_attempts=3,
        )
    clock.advance(seconds=61)
    await SendAccountInvitationHandler(uow_on(conn), clock, auth, TIMING).execute(notification_id)
    assert len(auth.calls) == 1
    assert (await state(conn, notification_id))[1]["attempts"] == 2


async def test_total_deadline_after_submission_is_unknown_and_does_not_resend(
    conn: asyncpg.Connection,
    world: World,
) -> None:
    clock, auth = FakeClock(), FakeAuth(conn)
    notification_id = await queue(conn, world, clock)
    auth.delay_s = 10
    handler = SendAccountInvitationHandler(
        uow_on(conn),
        clock,
        auth,
        replace(TIMING, total_timeout_s=0.2),
    )
    await handler.execute(notification_id)
    await handler.execute(notification_id)
    assert len(auth.calls) == 1
    assert (await state(conn, notification_id))[1]["outcome"] == "unknown"


async def test_competing_workers_share_the_project_sender_lease(pool: asyncpg.Pool) -> None:
    from nalar.infrastructure.db.uow import PgUnitOfWork
    from tests.integration.support.factories import build_world

    clock, sender_key = FakeClock(), str(uuid4())
    async with pool.acquire() as conn, conn.transaction():
        world = await build_world(conn)
        ids = [
            await queue(cast(asyncpg.Connection, conn), world, clock, user_id)
            for user_id in (world.teacher_id, world.student_id)
        ]

    async def claim(notification_id: UUID) -> PendingInvitation | None:
        async with PgUnitOfWork(pool.acquire) as uow:
            return await uow.activations.claim(
                notification_id,
                uuid4(),
                clock.now(),
                clock.now() + TIMING.lease,
                sender_key=sender_key,
                hourly_limit=20,
                daily_limit=200,
                sender_spacing=TIMING.sender_spacing,
                max_attempts=3,
            )

    try:
        results = await asyncio.gather(*(claim(notification_id) for notification_id in ids))
        assert sum(result is not None for result in results) == 1
    finally:
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "delete from pgmq.q_nalar_default"
                " where message->>'notification_id' = any($1::text[])",
                [str(notification_id) for notification_id in ids],
            )
            await conn.execute("delete from notifications where id = any($1::uuid[])", ids)
            await conn.execute(
                "delete from account_activations where user_id = any($1::uuid[])",
                [world.teacher_id, world.student_id],
            )
            await conn.execute(
                "update profiles set onboarding_required=false where id = any($1::uuid[])",
                [world.teacher_id, world.student_id],
            )


async def test_minute_recovery_and_default_consumer_deliver_an_invitation_once(
    conn: asyncpg.Connection,
    world: World,
) -> None:
    from dishka import Scope, provide

    from nalar.application.features.onboarding.messages import invitation_message
    from nalar.application.ports.auth_admin import AuthAdmin
    from nalar.bootstrap.container import build_container
    from nalar.bootstrap.settings import Settings
    from nalar.presentation.worker.consumers.default import default_handlers
    from nalar.presentation.worker.tick import cron_handlers
    from tests.integration.support.api import HarnessAdapters
    from tests.unit.application.fakes import (
        FakeIdentityProvider,
        FakeStorage,
        RecordingBackground,
        ScriptedAiGateway,
    )

    clock, auth = FakeClock(), FakeAuth(conn)
    notification_id = await queue(conn, world, clock)
    await conn.execute(
        "delete from pgmq.q_nalar_default where message->>'notification_id' = $1",
        str(notification_id),
    )

    class AccountAdapters(HarnessAdapters):
        @provide(scope=Scope.APP)
        def admin(self) -> AuthAdmin:
            return auth

    container = build_container(
        Settings(
            _env_file=None,
            env="test",
            account_email_enabled=True,
            account_email_activation_url="https://nalar.test/activate",
            cors_origins=["https://nalar.test"],
        ),
        AccountAdapters(
            conn,
            clock,
            ScriptedAiGateway(),
            RecordingBackground(),
            FakeIdentityProvider(),
            FakeStorage(),
        ),
    )
    try:
        await cron_handlers(container)["scheduler_tick"]({})
        assert (
            await conn.fetchval(
                "select count(*) from pgmq.q_nalar_default where message->>'notification_id' = $1",
                str(notification_id),
            )
            == 1
        )
        for _ in range(2):
            await default_handlers(container)["account_invitation"](
                invitation_message(notification_id)
            )
        assert len(auth.calls) == 1
    finally:
        await container.close()
