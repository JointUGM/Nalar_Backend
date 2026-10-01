from datetime import timedelta

import asyncpg
import pytest

from nalar.application.features.scheduler.commands.weekly_digest import (
    DigestTiming,
    WeeklyDigestHandler,
)
from nalar.application.ports.mailer import MailerError
from nalar.infrastructure.email.resend import DisabledMailer
from tests.integration.support.factories import World, evaluate, finish_session
from tests.integration.support.uow import uow_on
from tests.unit.application.fakes import FakeClock, FakeMailer

TIMING = DigestTiming(timedelta(seconds=60), timedelta(hours=23), 5)

SUMMARY = "Ananda menjelaskan gaya gesek dengan contoh dari rumah."


@pytest.fixture(autouse=True)
async def isolate_digest_recipient(conn: asyncpg.Connection, world: World) -> None:
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
    handler = WeeklyDigestHandler(uow_on(conn), FakeClock(), mailer, TIMING)
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
    handler = WeeklyDigestHandler(uow_on(conn), FakeClock(), mailer, TIMING)
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
    await WeeklyDigestHandler(uow_on(conn), FakeClock(), mailer, TIMING).execute()
    await conn.execute(
        "update profiles set weekly_digest_enabled = true, has_real_email = false where id = $1",
        world.parent_id,
    )
    await WeeklyDigestHandler(uow_on(conn), FakeClock(), mailer, TIMING).execute()
    assert mail_to(mailer, world) == []


class AcceptedThenTimedOutMailer(FakeMailer):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[tuple[str, str, str, str]] = []

    async def send(self, to: str, subject: str, text: str, *, idempotency_key: str) -> None:
        self.requests.append((to, subject, text, idempotency_key))
        await super().send(to, subject, text, idempotency_key=idempotency_key)
        if len(self.requests) == 1:
            raise MailerError()


async def test_ambiguous_delivery_retries_the_identical_request_once(
    conn: asyncpg.Connection,
    world: World,
) -> None:
    await summarized(conn, world)
    await release(conn, world)
    mailer = AcceptedThenTimedOutMailer()
    handler = WeeklyDigestHandler(uow_on(conn), FakeClock(), mailer, TIMING)
    with pytest.raises(MailerError):
        await handler.execute()
    await conn.execute(
        "update missions set title = 'Renamed mission' where id = $1", world.mission_id
    )
    await handler.execute()
    assert mailer.requests[0] == mailer.requests[1]
    assert len(mail_to(mailer, world)) == 1
    assert (
        await conn.fetchval(
            "select status::text from notifications where recipient_id = $1",
            world.parent_id,
        )
        == "sent"
    )


@pytest.mark.parametrize("change", ["opt_out", "unlink", "cancel", "ineligible"])
async def test_retry_rechecks_parent_visibility_and_preferences(
    conn: asyncpg.Connection,
    world: World,
    change: str,
) -> None:
    await summarized(conn, world)
    await release(conn, world)
    mailer = AcceptedThenTimedOutMailer()
    handler = WeeklyDigestHandler(uow_on(conn), FakeClock(), mailer, TIMING)
    with pytest.raises(MailerError):
        await handler.execute()
    if change == "opt_out":
        await conn.execute(
            "update profiles set weekly_digest_enabled = false where id = $1", world.parent_id
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
    await handler.execute()
    assert len(mailer.requests) == 1
    assert (
        await conn.fetchval(
            "select status::text from notifications where recipient_id = $1",
            world.parent_id,
        )
        == "failed"
    )


async def test_delivery_outside_provider_idempotency_window_is_not_retried(
    conn: asyncpg.Connection,
    world: World,
) -> None:
    await summarized(conn, world)
    await release(conn, world)
    clock = FakeClock()
    mailer = AcceptedThenTimedOutMailer()
    handler = WeeklyDigestHandler(uow_on(conn), clock, mailer, TIMING)
    with pytest.raises(MailerError):
        await handler.execute()
    clock.advance(hours=23)
    await handler.execute()
    assert len(mailer.requests) == 1


async def test_claim_excludes_other_workers_and_fences_stale_completion(
    conn: asyncpg.Connection,
    world: World,
) -> None:
    from uuid import uuid4

    from nalar.infrastructure.db.repositories.notifications import PgNotificationsRepo

    repo = PgNotificationsRepo(conn)
    now = FakeClock().now()
    await repo.queue_digest(world.parent_id, world.school_id, f"claim:{world.parent_id}", [], now)
    [digest] = [r for r in await repo.pending_digests(now) if r.recipient_id == world.parent_id]
    first, second = uuid4(), uuid4()
    assert await repo.claim_digest(digest.id, now, now + TIMING.lease, first, {}) is not None
    assert await repo.claim_digest(digest.id, now, now + TIMING.lease, second, {}) is None
    later = now + TIMING.lease
    assert await repo.claim_digest(digest.id, later, later + TIMING.lease, second, {}) is not None
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
