import asyncpg
import pytest

from tests.integration.support.factories import (
    World,
    create_concept,
    create_misconception,
    create_mission_version,
)


async def test_published_mission_version_cannot_change(
    conn: asyncpg.Connection, world: World
) -> None:
    with pytest.raises(asyncpg.RaiseError, match="is locked"):
        await conn.execute(
            "update mission_versions set anchor_problem = 'Soal baru' where id = $1",
            world.version_id,
        )


async def test_misconception_cannot_be_approved_before_its_concept(
    conn: asyncpg.Connection, world: World
) -> None:
    concept_id = await create_concept(conn, world.school_id, world.kb_id, review_status="pending")
    with pytest.raises(asyncpg.CheckViolationError, match="after its concept is approved"):
        await create_misconception(
            conn, world.school_id, world.kb_id, concept_id, review_status="approved"
        )


async def test_pending_concept_cannot_be_attached_to_a_mission_version(
    conn: asyncpg.Connection, world: World
) -> None:
    pending_id = await create_concept(conn, world.school_id, world.kb_id, review_status="pending")
    _, version_id = await create_mission_version(
        conn, world.school_id, world.kb_id, world.teacher_id
    )
    with pytest.raises(asyncpg.CheckViolationError, match="only approved, active items"):
        await conn.execute(
            "insert into mission_version_concepts (mission_version_id, concept_id) values ($1, $2)",
            version_id,
            pending_id,
        )
