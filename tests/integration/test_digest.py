from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import asyncpg
import pytest
from dishka import Scope, provide

from nalar.application.features.scheduler.commands.deliver_digest import DeliverDigestHandler
from nalar.application.features.scheduler.commands.weekly_digest import (
    DigestTiming,
    WeeklyDigestHandler,
)
from nalar.application.features.scheduler.messages import digest_delivery_message
from nalar.application.ports.mailer import BeforeSubmit, Mailer, MailerError, MailSubmission
from nalar.application.ports.queue import DEFAULT_QUEUE
from nalar.bootstrap.container import build_container
from nalar.bootstrap.settings import Settings
from nalar.infrastructure.db.repositories.notifications import PgNotificationsRepo
from nalar.infrastructure.email.smtp import DisabledMailer
from nalar.presentation.worker.consumers.default import default_handlers
from nalar.presentation.worker.tick import cron_handlers
from tests.integration.support.api import HarnessAdapters
from tests.integration.support.factories import (
    World,
    build_world,
    evaluate,
    finish_session,
    link_parent,
)
from tests.integration.support.uow import uow_on
from tests.unit.application.fakes import (
    FakeClock,
    FakeIdentityProvider,
    FakeMailer,
    FakeStorage,
    RecordingBackground,
    ScriptedAiGateway,
)

TIMING = DigestTiming(
    lease=timedelta(seconds=180),
    retry_window=timedelta(hours=48),
    max_attempts=5,
    dispatch_ttl=timedelta(seconds=1080),
    retry_delays=tuple(timedelta(seconds=v) for v in (1800, 7200, 21600, 43200)),
    daily_limit=200,
    batch_size=32,
    sender_spacing=timedelta(seconds=1),
    quota_cooldown=timedelta(days=1),
    auth_cooldown=timedelta(minutes=30),
)

SUMMARY = "Ananda menjelaskan gaya gesek dengan contoh dari rumah."


async def test_digest_aggregates_linked_children_across_schools_privately(
    conn: asyncpg.Connection,
    world: World,
) -> None:
    second, unrelated = await build_world(conn), await build_world(conn)
    for child in (world, second, unrelated):
        await summarized(conn, child)
        await release(conn, child)
    await conn.execute(
        "update profiles set weekly_digest_enabled = false where id = any($1::uuid[])",
        [second.parent_id, unrelated.parent_id],
    )
    await link_parent(conn, second.school_id, world.parent_id, second.student_id)
    await conn.execute(
        "update parent_summaries set content = $2 where student_id = $1",
        second.student_id,
        "Ringkasan anak sekolah kedua.",
    )
    await conn.execute(
        "update parent_summaries set content = $2 where student_id = $1",
        unrelated.student_id,
        "Rahasia anak yang tidak terhubung.",
    )
    mailer = FakeMailer()
    await dispatch(conn, FakeClock(), mailer)
    [body] = mail_to(mailer, world)
    assert SUMMARY in body and "Ringkasan anak sekolah kedua." in body
    assert "Rahasia anak yang tidak terhubung." not in body
    assert len(mailer.sent) == 1


async def test_minute_tick_queues_delivery_and_default_consumer_submits(
    conn: asyncpg.Connection,
    world: World,
) -> None:
    await summarized(conn, world)
    await release(conn, world)
    clock, mailer = FakeClock(), FakeMailer()
    await WeeklyDigestHandler(uow_on(conn), clock, mailer, TIMING).execute()
    await conn.execute(
        "delete from pgmq.q_nalar_default where message->>'kind' = 'digest_delivery'"
    )
    clock.advance(seconds=1080)

    class EmailAdapters(HarnessAdapters):
        @provide(scope=Scope.APP)
        def mail(self) -> Mailer:
            return mailer

    adapters = EmailAdapters(
        conn,
        clock,
        ScriptedAiGateway(),
        RecordingBackground(),
        FakeIdentityProvider(),
        FakeStorage(),
    )
    container = build_container(Settings(_env_file=None, env="test"), adapters)
    try:
        await cron_handlers(container)["scheduler_tick"]({})
        assert not mailer.sent
        assert (
            await conn.fetchval(
                "select count(*) from pgmq.q_nalar_default"
                " where message->>'kind' = 'digest_delivery'"
            )
            == 1
        )
        digest_id = await conn.fetchval(
            "select id from notifications where recipient_id = $1"
            " and type = 'parent_periodic_summary'",
            world.parent_id,
        )
        await default_handlers(container)["digest_delivery"](digest_delivery_message(digest_id))
        assert len(mail_to(mailer, world)) == 1
    finally:
        await container.close()


async def test_submitting_lease_expiry_is_terminal(conn: asyncpg.Connection, world: World) -> None:
    from nalar.infrastructure.db.repositories.notifications import PgNotificationsRepo

    repo = PgNotificationsRepo(conn)
    now = FakeClock().now()
    await repo.queue_digest(
        world.parent_id,
        world.school_id,
        f"smtp:{world.parent_id}",
        [],
        now,
        expires_at=now + TIMING.retry_window,
    )
    [digest] = [d for d in await repo.pending_digests(now) if d.recipient_id == world.parent_id]
    token = uuid4()
    assert (
        await repo.claim_digest(
            digest.id,
            now,
            now + TIMING.lease,
            token,
            {},
            sender_key="test-sender",
            daily_limit=TIMING.daily_limit,
            sender_spacing=TIMING.sender_spacing,
        )
        is not None
    )
    assert await repo.mark_digest_submitting(digest.id, token, now, "test-sender")
    assert not await repo.mark_digest_submitting(digest.id, token, now, "test-sender")
    assert (
        await repo.claim_digest(
            digest.id,
            now + TIMING.lease,
            now + TIMING.lease * 2,
            uuid4(),
            {},
            sender_key="test-sender",
            daily_limit=TIMING.daily_limit,
            sender_spacing=TIMING.sender_spacing,
        )
        is None
    )
    assert (
        await conn.fetchval(
            "select payload->>'outcome' from notifications where id = $1", digest.id
        )
        == "unknown"
    )


@pytest.fixture(autouse=True)
async def isolate_digest_recipient(conn: asyncpg.Connection, world: World) -> None:
    await conn.execute("delete from notifications where type = 'parent_periodic_summary'")
    await conn.execute(
        "update profiles set weekly_digest_enabled = false where id <> $1",
        world.parent_id,
    )


async def summarized(conn: asyncpg.Connection, world: World) -> None:
    await finish_session(conn, world, world.session_id)
    await evaluate(conn, world, world.session_id)
    await conn.execute(
        "insert into parent_summaries (school_id, publication_id, student_id, content)"
        " values ($1, $2, $3, $4)",
        world.school_id,
        world.publication_id,
        world.student_id,
        SUMMARY,
    )


async def release(conn: asyncpg.Connection, world: World) -> None:
    await conn.execute(
        "update publications set released_to_parents_at = now(), released_by = $2 where id = $1",
        world.publication_id,
        world.teacher_id,
    )


def mail_to(mailer: FakeMailer, world: World) -> list[str]:
    return [text for to, _, text in mailer.sent if to == f"{world.parent_id}@test.nalar"]


async def test_digest_skips_unreleased_and_ineligible_summaries(
    conn: asyncpg.Connection, world: World
) -> None:
    await summarized(conn, world)
    mailer = FakeMailer()
    handler = DispatchingDigest(conn, FakeClock(), mailer)
    await handler.execute()
    assert mail_to(mailer, world) == []
    assert (
        await conn.fetchval(
            "select count(*) from notifications where recipient_id = $1", world.parent_id
        )
        == 0
    )
    await release(conn, world)
    await handler.execute()
    [text] = mail_to(mailer, world)
    assert SUMMARY in text


async def test_rerun_in_the_same_week_sends_nothing_new(
    conn: asyncpg.Connection, world: World
) -> None:
    await summarized(conn, world)
    await release(conn, world)
    mailer = FakeMailer()
    handler = DispatchingDigest(conn, FakeClock(), mailer)
    await handler.execute()
    await handler.execute()
    assert len(mail_to(mailer, world)) == 1


async def test_disabled_preference_and_parents_without_email_get_nothing(
    conn: asyncpg.Connection, world: World
) -> None:
    await summarized(conn, world)
    await release(conn, world)
    await conn.execute(
        "update profiles set weekly_digest_enabled = false where id = $1", world.parent_id
    )
    mailer = FakeMailer()
    await dispatch(conn, FakeClock(), mailer)
    await conn.execute(
        "update profiles set weekly_digest_enabled = true, has_real_email = false where id = $1",
        world.parent_id,
    )
    await dispatch(conn, FakeClock(), mailer)
    assert mail_to(mailer, world) == []


class AcceptedThenTimedOutMailer(FakeMailer):
    async def send(self, submission: MailSubmission, *, before_submit: BeforeSubmit) -> bool:
        await super().send(submission, before_submit=before_submit)
        raise MailerError(acceptance_unknown=True, category="acceptance_unknown")


class RejectedOnceMailer(FakeMailer):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[MailSubmission] = []

    async def send(self, submission: MailSubmission, *, before_submit: BeforeSubmit) -> bool:
        self.requests.append(submission)
        if len(self.requests) == 1:
            assert await before_submit()
            raise MailerError(retryable=True, category="smtp_rejected", smtp_code=451)
        return await super().send(submission, before_submit=before_submit)


async def dispatch(conn: asyncpg.Connection, clock: FakeClock, mailer: FakeMailer) -> None:
    from nalar.infrastructure.db.repositories.notifications import PgNotificationsRepo

    await WeeklyDigestHandler(uow_on(conn), clock, mailer, TIMING).execute()
    pending = await PgNotificationsRepo(conn).pending_digests(clock.now())
    for digest in pending:
        await DeliverDigestHandler(uow_on(conn), clock, mailer, TIMING).execute(digest.id)


class DispatchingDigest:
    def __init__(self, conn: asyncpg.Connection, clock: FakeClock, mailer: FakeMailer) -> None:
        self.conn, self.clock, self.mailer = conn, clock, mailer

    async def execute(self) -> None:
        await dispatch(self.conn, self.clock, self.mailer)


async def test_unknown_acceptance_is_never_automatically_retried(
    conn: asyncpg.Connection,
    world: World,
) -> None:
    await summarized(conn, world)
    await release(conn, world)
    clock, mailer = FakeClock(), AcceptedThenTimedOutMailer()
    await dispatch(conn, clock, mailer)
    clock.advance(hours=24)
    await dispatch(conn, clock, mailer)
    assert len(mail_to(mailer, world)) == 1
    row = await conn.fetchrow(
        "select status::text, payload->>'outcome' as outcome from notifications"
        " where recipient_id = $1",
        world.parent_id,
    )
    assert row and row["status"] == "failed" and row["outcome"] == "unknown"


async def test_definitive_rejection_retries_the_frozen_message(
    conn: asyncpg.Connection,
    world: World,
) -> None:
    await summarized(conn, world)
    await release(conn, world)
    clock, mailer = FakeClock(), RejectedOnceMailer()
    await dispatch(conn, clock, mailer)
    await conn.execute(
        "update missions set title = 'Renamed mission' where id = $1", world.mission_id
    )
    clock.advance(minutes=30)
    await dispatch(conn, clock, mailer)
    assert mailer.requests[0] == mailer.requests[1]
    assert len(mail_to(mailer, world)) == 1


async def revoke(conn: asyncpg.Connection, world: World, change: str) -> None:
    if change == "opt_out":
        await conn.execute(
            "update profiles set weekly_digest_enabled = false where id = $1", world.parent_id
        )
    elif change == "email":
        await conn.execute(
            "update profiles set contact_email = 'changed@example.test' where id = $1",
            world.parent_id,
        )
    elif change == "unlink":
        await conn.execute("delete from parent_student_links where parent_id = $1", world.parent_id)
    elif change == "cancel":
        await conn.execute(
            "update publications set cancelled_at = now() where id = $1", world.publication_id
        )
    else:
        await conn.execute(
            "update session_evaluations set status = 'failed' where session_id = $1",
            world.session_id,
        )


@pytest.mark.parametrize("change", ["opt_out", "email", "unlink", "cancel", "ineligible"])
async def test_retry_rechecks_parent_visibility_and_preferences(
    conn: asyncpg.Connection,
    world: World,
    change: str,
) -> None:
    await summarized(conn, world)
    await release(conn, world)
    clock, mailer = FakeClock(), RejectedOnceMailer()
    await dispatch(conn, clock, mailer)
    await revoke(conn, world, change)
    clock.advance(minutes=30)
    await dispatch(conn, clock, mailer)
    assert len(mailer.requests) == 1
    assert (
        await conn.fetchval(
            "select status::text from notifications where recipient_id = $1", world.parent_id
        )
        == "failed"
    )


async def test_expired_delivery_is_not_retried(conn: asyncpg.Connection, world: World) -> None:
    await summarized(conn, world)
    await release(conn, world)
    clock, mailer = FakeClock(), RejectedOnceMailer()
    await dispatch(conn, clock, mailer)
    clock.advance(hours=48)
    await dispatch(conn, clock, mailer)
    assert len(mailer.requests) == 1


async def test_claim_excludes_other_workers_and_fences_stale_completion(
    conn: asyncpg.Connection,
    world: World,
) -> None:
    from uuid import uuid4

    from nalar.infrastructure.db.repositories.notifications import PgNotificationsRepo

    repo = PgNotificationsRepo(conn)
    now = FakeClock().now()
    await repo.queue_digest(
        world.parent_id,
        world.school_id,
        f"claim:{world.parent_id}",
        [],
        now,
        expires_at=now + TIMING.retry_window,
    )
    [digest] = [r for r in await repo.pending_digests(now) if r.recipient_id == world.parent_id]
    first, second = uuid4(), uuid4()
    assert (
        await repo.claim_digest(
            digest.id,
            now,
            now + TIMING.lease,
            first,
            {},
            sender_key="test-sender",
            daily_limit=TIMING.daily_limit,
            sender_spacing=TIMING.sender_spacing,
        )
        is not None
    )
    assert (
        await repo.claim_digest(
            digest.id,
            now,
            now + TIMING.lease,
            second,
            {},
            sender_key="test-sender",
            daily_limit=TIMING.daily_limit,
            sender_spacing=TIMING.sender_spacing,
        )
        is None
    )
    later = now + TIMING.lease
    assert (
        await repo.claim_digest(
            digest.id,
            later,
            later + TIMING.lease,
            second,
            {},
            sender_key="test-sender",
            daily_limit=TIMING.daily_limit,
            sender_spacing=TIMING.sender_spacing,
        )
        is not None
    )
    await repo.finish_digest(digest.id, first, "sent", later)
    assert (
        await conn.fetchval("select status::text from notifications where id = $1", digest.id)
        == "pending"
    )
    await repo.finish_digest(digest.id, second, "sent", later)
    assert (
        await conn.fetchval("select status::text from notifications where id = $1", digest.id)
        == "sent"
    )


async def test_unconfigured_mailer_does_not_create_delivery_records(
    conn: asyncpg.Connection,
    world: World,
) -> None:
    await summarized(conn, world)
    await release(conn, world)
    assert (
        await WeeklyDigestHandler(uow_on(conn), FakeClock(), DisabledMailer(), TIMING).execute()
        == 0
    )

    assert (
        await conn.fetchval(
            "select count(*) from notifications where recipient_id = $1",
            world.parent_id,
        )
        == 0
    )


@pytest.mark.parametrize("change", ["opt_out", "email", "unlink", "cancel", "ineligible"])
async def test_submission_guard_rechecks_changes_during_smtp_setup(
    conn: asyncpg.Connection,
    world: World,
    change: str,
) -> None:
    class ChangingMailer(FakeMailer):
        async def send(self, submission: MailSubmission, *, before_submit: BeforeSubmit) -> bool:
            await revoke(conn, world, change)
            return await super().send(submission, before_submit=before_submit)

    await summarized(conn, world)
    await release(conn, world)
    mailer = ChangingMailer()
    await dispatch(conn, FakeClock(), mailer)
    assert not mailer.sent
    assert (
        await conn.fetchval(
            "select payload->>'category' from notifications where recipient_id = $1",
            world.parent_id,
        )
        == "visibility_changed"
    )


@pytest.mark.parametrize("stage,delivered", [("before", 1), ("checkpoint", 0), ("accepted", 1)])
async def test_worker_crash_recovers_without_repeating_possible_acceptance(
    conn: asyncpg.Connection,
    world: World,
    stage: str,
    delivered: int,
) -> None:
    class CrashingMailer(FakeMailer):
        crashed = False

        async def send(self, submission: MailSubmission, *, before_submit: BeforeSubmit) -> bool:
            if self.crashed:
                return await super().send(submission, before_submit=before_submit)
            self.crashed = True
            if stage == "accepted":
                await super().send(submission, before_submit=before_submit)
            elif stage == "checkpoint":
                assert await before_submit()
            raise RuntimeError("worker stopped")

    await summarized(conn, world)
    await release(conn, world)
    clock, mailer = FakeClock(), CrashingMailer()
    with pytest.raises(RuntimeError, match="worker stopped"):
        await dispatch(conn, clock, mailer)
    clock.advance(seconds=181)
    await dispatch(conn, clock, mailer)
    assert len(mailer.sent) == delivered
    status = await conn.fetchval(
        "select status::text from notifications where recipient_id = $1", world.parent_id
    )
    assert status == ("sent" if stage == "before" else "failed")


async def test_dispatch_reservation_recovers_a_lost_queue_message(
    conn: asyncpg.Connection,
    world: World,
) -> None:
    await summarized(conn, world)
    await release(conn, world)
    clock = FakeClock()
    handler = WeeklyDigestHandler(uow_on(conn), clock, FakeMailer(), TIMING)
    assert await handler.execute() == 1
    assert await handler.dispatch_due() == 0
    await conn.execute(
        "delete from pgmq.q_nalar_default where message->>'kind' = 'digest_delivery'"
    )
    clock.advance(seconds=1080)
    assert await handler.dispatch_due() == 1
    assert (
        await conn.fetchval(
            "select count(*) from pgmq.q_nalar_default where message->>'kind' = 'digest_delivery'"
        )
        == 1
    )


async def test_queue_insertion_and_dispatch_reservation_roll_back_together(
    conn: asyncpg.Connection,
    world: World,
) -> None:
    repo, clock = PgNotificationsRepo(conn), FakeClock()
    await repo.queue_digest(
        world.parent_id,
        world.school_id,
        f"rollback:{world.parent_id}",
        [],
        clock.now(),
        expires_at=clock.now() + TIMING.retry_window,
    )
    with pytest.raises(RuntimeError, match="abort dispatch"):
        async with uow_on(conn) as uow:
            [digest_id] = await uow.notifications.reserve_due_digests(
                clock.now(), clock.now() + TIMING.dispatch_ttl, 32
            )
            await uow.queue.send(DEFAULT_QUEUE, digest_delivery_message(digest_id))
            raise RuntimeError("abort dispatch")
    assert (
        await conn.fetchval(
            "select payload->>'dispatch_until' from notifications where recipient_id = $1",
            world.parent_id,
        )
        is None
    )


async def test_legacy_attempted_delivery_is_quarantined(
    conn: asyncpg.Connection, world: World
) -> None:
    now = FakeClock().now()
    await conn.execute(
        "insert into notifications"
        " (recipient_id, school_id, type, dedupe_key, payload, scheduled_for)"
        " values ($1, $2, 'parent_periodic_summary', $3, '{\"attempts\": 1}', $4)",
        world.parent_id,
        world.school_id,
        f"legacy:{world.parent_id}",
        now,
    )
    assert not await PgNotificationsRepo(conn).reserve_due_digests(
        now, now + TIMING.dispatch_ttl, 32
    )
    assert (
        await conn.fetchval(
            "select payload->>'outcome' from notifications where recipient_id = $1", world.parent_id
        )
        == "unknown"
    )


@pytest.mark.parametrize("action", ["cooldown", "suspend"])
async def test_sender_hold_defers_other_messages_without_consuming_attempts(
    conn: asyncpg.Connection,
    world: World,
    action: str,
) -> None:
    now, repo = FakeClock().now(), PgNotificationsRepo(conn)
    for key in ("first", "second"):
        await repo.queue_digest(
            world.parent_id,
            world.school_id,
            f"{key}:{world.parent_id}",
            [],
            now,
            expires_at=now + TIMING.retry_window,
        )
    first, second = await repo.pending_digests(now)
    token = uuid4()
    assert await repo.claim_digest(
        first.id,
        now,
        now + TIMING.lease,
        token,
        {},
        sender_key="test-sender",
        daily_limit=TIMING.daily_limit,
        sender_spacing=TIMING.sender_spacing,
    )
    await repo.finish_digest(
        first.id,
        token,
        "failed",
        now,
        outcome="rejected",
        sender_hold_until=now + timedelta(days=1) if action == "cooldown" else None,
        sender_suspended=action == "suspend",
    )
    assert (
        await repo.claim_digest(
            second.id,
            now,
            now + TIMING.lease,
            uuid4(),
            {},
            sender_key="test-sender",
            daily_limit=TIMING.daily_limit,
            sender_spacing=TIMING.sender_spacing,
        )
        is None
    )
    payload = await repo.get_digest(second.id)
    assert payload and payload.payload.get("attempts", 0) == 0
    later = now + timedelta(days=1)
    assert bool(
        await repo.claim_digest(
            second.id,
            later,
            later + TIMING.lease,
            uuid4(),
            {},
            sender_key="test-sender",
            daily_limit=TIMING.daily_limit,
            sender_spacing=TIMING.sender_spacing,
        )
    ) is (action == "cooldown")


async def test_daily_budget_counts_unknown_submissions(
    conn: asyncpg.Connection, world: World
) -> None:
    now, repo = FakeClock().now(), PgNotificationsRepo(conn)
    for key in ("budget-a", "budget-b"):
        await repo.queue_digest(
            world.parent_id,
            world.school_id,
            f"{key}:{world.parent_id}",
            [],
            now,
            expires_at=now + TIMING.retry_window,
        )
    first, second = await repo.pending_digests(now)
    token = uuid4()
    assert await repo.claim_digest(
        first.id,
        now,
        now + TIMING.lease,
        token,
        {},
        daily_limit=1,
        sender_key="test-sender",
        sender_spacing=TIMING.sender_spacing,
    )

    assert await repo.mark_digest_submitting(first.id, token, now, "test-sender")
    await repo.finish_digest(first.id, token, "failed", now, outcome="unknown")
    assert (
        await repo.claim_digest(
            second.id,
            now,
            now + TIMING.lease,
            uuid4(),
            {},
            daily_limit=1,
            sender_key="test-sender",
            sender_spacing=TIMING.sender_spacing,
        )
        is None
    )
    later = now + timedelta(days=1)
    assert await repo.claim_digest(
        second.id,
        later,
        later + TIMING.lease,
        uuid4(),
        {},
        daily_limit=1,
        sender_key="test-sender",
        sender_spacing=TIMING.sender_spacing,
    )


async def test_one_attempt_policy_records_rejection_without_retry_delays(
    conn: asyncpg.Connection,
    world: World,
) -> None:
    await summarized(conn, world)
    await release(conn, world)
    clock, mailer = FakeClock(), RejectedOnceMailer()
    timing = replace(TIMING, max_attempts=1, retry_delays=())
    await WeeklyDigestHandler(uow_on(conn), clock, mailer, timing).execute()
    [digest] = await PgNotificationsRepo(conn).pending_digests(clock.now())
    await DeliverDigestHandler(uow_on(conn), clock, mailer, timing).execute(digest.id)
    assert (
        await conn.fetchval("select status::text from notifications where id = $1", digest.id)
        == "failed"
    )
    assert (
        await conn.fetchval(
            "select payload->>'outcome' from notifications where id = $1", digest.id
        )
        == "rejected"
    )
