import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import asyncpg

from nalar.bootstrap.seed import seed, seed_trail_demo
from tests.integration.support.api import api_client, as_user

CONTENT: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[2] / "supabase/seed/gaya_dan_gerak.json").read_text(
        encoding="utf-8"
    )
)
TABLES = [
    "schools",
    "profiles",
    "school_memberships",
    "student_profiles",
    "class_enrollments",
    "teaching_assignments",
    "parent_student_links",
    "knowledge_bases",
    "concepts",
    "misconceptions",
    "missions",
    "mission_versions",
    "mission_version_concepts",
    "mission_version_misconceptions",
    "publications",
    "sessions",
    "session_concept_results",
]


class FakeUsers:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn
        self.ids: dict[str, UUID] = {}

    async def ensure_user(self, email: str, password: str, full_name: str) -> UUID:
        if email not in self.ids:
            existing = await self._conn.fetchval(
                "select id from auth.users where email = $1", email
            )
            self.ids[email] = existing or uuid4()
            if existing is None:
                await self._conn.execute(
                    "insert into auth.users (id, email) values ($1, $2)", self.ids[email], email
                )
        return self.ids[email]


class FakeBuckets:
    def __init__(self) -> None:
        self.names: set[str] = set()

    async def ensure_bucket(self, name: str) -> None:
        self.names.add(name)


async def counts(conn: asyncpg.Connection) -> dict[str, int]:
    return {t: await conn.fetchval(f"select count(*) from {t}") for t in TABLES}


async def test_seed_twice_gives_identical_row_counts(conn: asyncpg.Connection) -> None:
    users, buckets = FakeUsers(conn), FakeBuckets()
    first = await seed(conn, CONTENT, users, buckets, password="demo-password")
    after_first = await counts(conn)
    second = await seed(conn, CONTENT, users, buckets, password="demo-password")
    assert await counts(conn) == after_first
    assert first.version_id == second.version_id
    assert buckets.names == {"teaching-materials", "roster-imports"}


async def test_seeded_version_is_reviewed_with_a_valid_pack(conn: asyncpg.Connection) -> None:
    report = await seed(conn, CONTENT, FakeUsers(conn), FakeBuckets(), password="x")
    row = await conn.fetchrow(
        "select reviewed_at, context_pack, max_turns, max_duration_minutes"
        " from mission_versions where id = $1",
        report.version_id,
    )
    assert row is not None
    assert row["reviewed_at"] is not None
    assert json.loads(row["context_pack"]) == report.pack
    assert (row["max_turns"], row["max_duration_minutes"]) == (6, 20)


async def test_trail_demo_is_idempotent_and_the_endpoint_reports_the_planned_numbers(
    conn: asyncpg.Connection,
) -> None:
    await seed(conn, CONTENT, FakeUsers(conn), FakeBuckets(), password="x")
    await seed_trail_demo(conn, CONTENT)
    after_first = await counts(conn)
    await seed_trail_demo(conn, CONTENT)
    assert await counts(conn) == after_first
    teacher_id = await conn.fetchval(
        "select id from profiles where contact_email = $1", CONTENT["users"]["teacher"]["email"]
    )
    later = await conn.fetchval(
        "select p.id from publications p join mission_versions v on v.id = p.mission_version_id"
        " join missions m on m.id = v.mission_id where m.title like '% lanjutan (contoh)'"
    )
    async with api_client(conn) as api:
        response = await api.get(
            f"/publications/{later}/misconception-trail", headers=as_user(teacher_id)
        )
    [item] = response.json()["items"]
    assert (item["held_count"], item["resolved_count"], item["still_held_count"]) == (14, 9, 4)
    assert item["not_observed_count"] == 1
