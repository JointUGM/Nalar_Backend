from uuid import uuid4

import asyncpg

from nalar.infrastructure.db.repositories.jobs import PgJobStore
from tests.integration.support.factories import World


async def test_job_lifecycle(conn: asyncpg.Connection, world: World) -> None:
    jobs = PgJobStore(conn)
    section_id = uuid4()
    job_id = await jobs.create(
        kind="kb_build_section",
        entity_type="material_section",
        entity_id=section_id,
        school_id=world.school_id,
        requested_by=world.teacher_id,
    )

    queued = await jobs.get(job_id)
    assert queued is not None
    assert (queued.status, queued.attempts, queued.entity_id) == ("queued", 0, section_id)

    assert await jobs.mark_running(job_id)
    await jobs.mark_failed(job_id, "ai_output_invalid", "step 5 failed twice")
    failed = await jobs.get(job_id)
    assert failed is not None
    assert (failed.status, failed.attempts, failed.error_code) == (
        "failed",
        1,
        "ai_output_invalid",
    )
    assert not await jobs.mark_running(job_id)

    other = await jobs.create(
        kind="kb_build_section",
        entity_type="material_section",
        entity_id=section_id,
        school_id=world.school_id,
        requested_by=world.teacher_id,
    )
    assert await jobs.mark_running(other)
    assert await jobs.mark_running(other)
    await jobs.mark_succeeded(other)
    succeeded = await jobs.get(other)
    assert succeeded is not None
    assert (succeeded.status, succeeded.attempts, succeeded.error_code) == ("succeeded", 2, None)
    assert not await jobs.mark_running(other)


async def test_unknown_job_is_none(conn: asyncpg.Connection) -> None:
    assert await PgJobStore(conn).get(uuid4()) is None
