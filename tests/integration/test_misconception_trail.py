import csv
import io
from datetime import UTC, datetime, timedelta
from uuid import UUID

import asyncpg

from nalar.bootstrap.kpi import teacher_cycle
from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import (
    World,
    build_world,
    create_class,
    create_misconception,
    create_mission_version,
    create_run,
    create_student,
    publish,
)


class Trail:
    def __init__(self, conn: asyncpg.Connection, world: World) -> None:
        self.conn = conn
        self.world = world
        self.concept = world.concept_ids[0]
        self.held: UUID
        self.earlier = world.publication_id
        self.earlier_run = world.run_id
        self.later: UUID
        self.later_run: UUID

    async def setup(self, *, later_pack: bool = True) -> "Trail":
        w = self.world
        self.held = await self.conn.fetchval(
            "select id from misconceptions where concept_id = $1", self.concept
        )
        await self.conn.execute(
            "update publications set created_at = now() - interval '7 days' where id = $1",
            self.earlier,
        )
        await self.conn.execute("delete from session_turns where session_id = $1", w.session_id)
        await self.conn.execute("delete from sessions where id = $1", w.session_id)
        _, version = await create_mission_version(
            self.conn, w.school_id, w.kb_id, w.teacher_id, context_pack=w.pack
        )
        await self.conn.execute(
            "insert into mission_version_concepts (mission_version_id, concept_id) values ($1, $2)",
            version,
            self.concept,
        )
        if later_pack:
            await self.conn.execute(
                "insert into mission_version_misconceptions (mission_version_id, misconception_id)"
                " values ($1, $2)",
                version,
                self.held,
            )
        self.later = await publish(self.conn, w.school_id, version, w.class_id, w.teacher_id)
        self.later_run = await create_run(self.conn, w.school_id, self.later)
        return self

    async def student(self) -> UUID:
        w = self.world
        return await create_student(self.conn, w.school_id, w.class_id, w.year_id)

    async def attempt(
        self,
        publication: UUID,
        student: UUID,
        outcome: str | None,
        misconception: UUID | None = None,
        *,
        number: int = 1,
        status: str = "completed",
        evaluated: bool = True,
    ) -> None:
        run = self.earlier_run if publication == self.earlier else self.later_run
        session = await self.conn.fetchval(
            "insert into sessions (school_id, publication_id, run_id, student_id, attempt_number,"
            " status, end_reason, started_at, ended_at, deadline_at)"
            " values ($1, $2, $3, $4, $5, $6::session_status,"
            " case when $6 = 'completed' then 'student_completed'::session_end_reason"
            " when $6 <> 'in_progress' then 'max_duration_reached'::session_end_reason end,"
            " now() - interval '30 minutes',"
            " case when $6 <> 'in_progress' then now() - interval '10 minutes' end,"
            " now() + interval '20 minutes') returning id",
            self.world.school_id,
            publication,
            run,
            student,
            number,
            status,
        )
        if not evaluated:
            return
        await self.conn.execute(
            "insert into session_evaluations (school_id, session_id) values ($1, $2)",
            self.world.school_id,
            session,
        )
        if outcome is not None:
            await self.conn.execute(
                "insert into session_concept_results"
                " (school_id, session_id, concept_id, outcome, misconception_id)"
                " values ($1, $2, $3, $4::concept_outcome, $5)",
                self.world.school_id,
                session,
                self.concept,
                outcome,
                misconception if outcome == "misconception" else None,
            )

    async def get(self, user: UUID, publication: UUID | None = None, query: str = "") -> dict:  # type: ignore[type-arg]
        async with api_client(self.conn) as api:
            response = await api.get(
                f"/publications/{publication or self.later}/misconception-trail{query}",
                headers=as_user(user),
            )
        assert response.status_code == 200, response.text
        return response.json()  # type: ignore[no-any-return]


async def test_trail_counts_each_holder_once_and_match_sql(
    conn: asyncpg.Connection, world: World
) -> None:
    t = await Trail(conn, world).setup()
    other = await create_misconception(conn, world.school_id, world.kb_id, t.concept, "approved")
    await conn.execute(
        "insert into mission_version_misconceptions (mission_version_id, misconception_id)"
        " select mission_version_id, $2 from publications where id = $1",
        t.later,
        other,
    )
    outcomes = [
        ("mastered", None),
        ("mastered", None),
        ("developing", None),
        ("misconception", other),
        ("misconception", t.held),
        ("misconception", t.held),
        ("not_observed", None),
    ]
    for outcome, misconception in outcomes:
        s = await t.student()
        await t.attempt(t.earlier, s, "misconception", t.held)
        await t.attempt(t.later, s, outcome, misconception)
    gone = await t.student()
    await t.attempt(t.earlier, gone, "misconception", t.held)
    body = await t.get(world.teacher_id)
    [item] = body["items"]
    count = (
        "select count(distinct s.student_id) from sessions s"
        " join session_concept_results r on r.session_id = s.id"
        " where s.publication_id = $1 and r.misconception_id = $2"
    )
    held = await conn.fetchval(count, t.earlier, t.held)
    still = await conn.fetchval(count, t.later, t.held)
    assert (item["held_count"], item["still_held_count"]) == (held, still) == (8, 2)
    assert item["resolved_breakdown"] == {"mastered": 2, "developing": 1, "other_misconception": 1}
    assert (item["resolved_count"], item["not_observed_count"]) == (4, 2)
    assert item["held_count"] == 4 + 2 + 2
    assert len(item["students_still_holding"]) == 2
    assert all(i["name"] == "Siswa Uji" for i in item["students_still_holding"])
    assert body["from"]["publication_id"] == str(t.earlier)
    assert (body["from"]["denominator"], body["to"]["denominator"]) == (8, 7)
    assert body["not_retested"] == []


async def test_unfinished_latest_attempt_never_falls_back_to_the_older_one(
    conn: asyncpg.Connection, world: World
) -> None:
    t = await Trail(conn, world).setup()
    s = await t.student()
    await t.attempt(t.earlier, s, "misconception", t.held)
    await t.attempt(t.later, s, "mastered", number=1)
    await t.attempt(t.later, s, None, number=2, status="timed_out", evaluated=False)
    [item] = (await t.get(world.teacher_id))["items"]
    assert (item["resolved_count"], item["not_observed_count"]) == (0, 1)


async def test_ineligible_earlier_attempt_does_not_make_a_holder(
    conn: asyncpg.Connection, world: World
) -> None:
    t = await Trail(conn, world).setup()
    a, b = await t.student(), await t.student()
    await t.attempt(t.earlier, a, "misconception", t.held)
    await t.attempt(t.earlier, b, None, status="in_progress", evaluated=False)
    await t.attempt(t.later, a, "mastered")
    await t.attempt(t.later, b, "mastered")
    [item] = (await t.get(world.teacher_id))["items"]
    assert item["held_count"] == 1


async def test_misconception_missing_from_the_later_pack_is_not_retested(
    conn: asyncpg.Connection, world: World
) -> None:
    t = await Trail(conn, world).setup(later_pack=False)
    s = await t.student()
    await t.attempt(t.earlier, s, "misconception", t.held)
    await t.attempt(t.later, s, "mastered")
    body = await t.get(world.teacher_id)
    assert body["items"] == []
    assert [(n["misconception_id"], n["held_count"]) for n in body["not_retested"]] == [
        (str(t.held), 1)
    ]


async def test_no_earlier_publication_gives_an_empty_trail(
    conn: asyncpg.Connection, world: World
) -> None:
    t = await Trail(conn, world).setup()
    s = await t.student()
    await t.attempt(t.earlier, s, "misconception", t.held)
    await conn.execute("update publications set cancelled_at = now() where id = $1", t.earlier)
    body = await t.get(world.teacher_id)
    assert (body["from"], body["items"], body["not_retested"]) == (None, [], [])
    assert body["to"]["publication_id"] == str(t.later)


async def test_named_earlier_publication_of_another_class_is_404(
    conn: asyncpg.Connection, world: World
) -> None:
    t = await Trail(conn, world).setup()
    other_class = await create_class(conn, world.school_id, world.year_id, "8B")
    elsewhere = await publish(
        conn, world.school_id, world.version_id, other_class, world.teacher_id
    )
    async with api_client(conn) as api:
        response = await api.get(
            f"/publications/{t.later}/misconception-trail?from_publication_id={elsewhere}",
            headers=as_user(world.teacher_id),
        )
    assert response.status_code == 404


async def test_only_the_teaching_teacher_reads_the_trail(
    conn: asyncpg.Connection, world: World
) -> None:
    t = await Trail(conn, world).setup()
    intruder = await build_world(conn, "SMP Penyusup")
    async with api_client(conn) as api:
        statuses = [
            (
                await api.get(f"/publications/{t.later}/misconception-trail", headers=as_user(user))
            ).status_code
            for user in (
                world.student_id,
                world.parent_id,
                world.admin_id,
                intruder.teacher_id,
            )
        ]
    assert statuses == [404, 404, 404, 404]


async def test_mgmp_package_has_class_counts_and_trail_but_no_student_identity(
    conn: asyncpg.Connection, world: World
) -> None:
    t = await Trail(conn, world).setup()
    s = await t.student()
    await t.attempt(t.earlier, s, "misconception", t.held)
    await t.attempt(t.later, s, "misconception", t.held)
    async with api_client(conn) as api:
        response = await api.get(
            f"/publications/{t.later}/mgmp-package", headers=as_user(world.teacher_id)
        )
        denied = await api.get(
            f"/publications/{t.later}/mgmp-package", headers=as_user(world.admin_id)
        )
    rows = list(csv.DictReader(io.StringIO(response.text.lstrip("﻿"))))
    row = next(r for r in rows if r["earlier_students_holding"])
    assert (row["students_holding"], row["earlier_students_holding"]) == ("1", "1")
    assert (row["earlier_still_holding"], row["earlier_resolved"]) == ("1", "0")
    assert str(s) not in response.text and "Siswa Uji" not in response.text
    assert denied.status_code == 404


async def test_kpi_aggregates_are_suppressed_below_three_classes(
    conn: asyncpg.Connection, world: World
) -> None:
    t = await Trail(conn, world).setup()
    s = await t.student()
    await t.attempt(t.earlier, s, "misconception", t.held)
    await t.attempt(t.later, s, "mastered")
    since = datetime.now(UTC) - timedelta(days=30)
    assert await teacher_cycle(conn, world.school_id, since) == {
        "suppressed": True,
        "min_group": 3,
    }
    for name in ("8B", "8C"):
        klass = await create_class(conn, world.school_id, world.year_id, name)
        await publish(conn, world.school_id, world.version_id, klass, world.teacher_id)
    report = await teacher_cycle(conn, world.school_id, since)
    assert report["suppressed"] is False
    assert (report["follow_up_pairs"], report["held"], report["no_longer_shown"]) == (1, 1, 1)
