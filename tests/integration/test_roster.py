from uuid import UUID, uuid4

import asyncpg
import pytest

from nalar.application.features.roster.commands.import_roster import (
    ImportInterrupted,
    ImportRosterHandler,
)
from nalar.application.features.roster.commands.upload_roster import RosterLimits
from nalar.application.ports.auth_admin import AuthAdminError
from nalar.infrastructure.db.repositories.roster import PgRosterRepo
from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import World, build_world, create_teacher
from tests.integration.support.uow import uow_on
from tests.unit.application.fakes import FakeClock, FakeStorage

LIMITS = RosterLimits(5 * 1024 * 1024, 120, "siswa.nalar.id")

HEADER = "role,full_name,email,nisn,class_name,grade_level,parent_email,parent_name,relationship"
ROSTER = (
    "\n".join(
        [
            HEADER,
            "student,Budi Santoso,,0099887766,9C,9,ibu.budi@mail.id,Sri,ibu",
            "student,Tanpa NISN,,,9C,9,,,",
            "teacher,Pak Joko,joko@smp.id,,,,,,",
        ]
    )
    + "\n"
)


class FakeAuthAdmin:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def create_or_find(self, email: str, full_name: str) -> UUID:
        existing: UUID | None = await self._conn.fetchval(
            "select id from auth.users where email = $1", email
        )
        if existing:
            return existing
        user_id = uuid4()
        await self._conn.execute(
            "insert into auth.users (id, email) values ($1, $2)", user_id, email
        )
        return user_id


async def upload(
    conn: asyncpg.Connection, world: World, storage: FakeStorage, text: str = ROSTER
) -> dict[str, str]:
    async with api_client(conn, storage=storage) as api:
        response = await api.post(
            f"/schools/{world.school_id}/roster-imports",
            data={"academic_year_id": str(world.year_id)},
            files={"file": ("roster.csv", text.encode(), "text/csv")},
            headers=as_user(world.admin_id),
        )
    assert response.status_code == 202, response.text
    body: dict[str, str] = response.json()
    return body


async def run(conn: asyncpg.Connection, storage: FakeStorage, body: dict[str, str]) -> None:
    handler = ImportRosterHandler(uow_on(conn), storage, FakeAuthAdmin(conn), FakeClock(), LIMITS)
    await handler.execute(UUID(body["import_id"]), UUID(body["job_id"]))


async def test_bad_rows_are_reported_while_valid_rows_import(
    conn: asyncpg.Connection, world: World
) -> None:
    storage = FakeStorage()
    body = await upload(conn, world, storage)
    await run(conn, storage, body)
    async with api_client(conn) as api:
        result = (
            await api.get(f"/roster-imports/{body['import_id']}", headers=as_user(world.admin_id))
        ).json()
    assert (
        result["status"],
        result["rows_total"],
        result["rows_succeeded"],
        result["rows_failed"],
    ) == ("completed", 3, 2, 1)
    assert [(e["row_number"], e["field"]) for e in result["errors"]] == [(3, "nisn")]
    student = await conn.fetchval("select user_id from student_profiles where nisn = '0099887766'")
    assert (
        await conn.fetchval(
            "select c.name from class_enrollments e join classes c on c.id = e.class_id"
            " where e.student_id = $1 and e.status = 'active'",
            student,
        )
        == "9C"
    )
    assert (
        await conn.fetchval(
            "select count(*) from parent_student_links where student_id = $1", student
        )
        == 1
    )


async def test_reimporting_the_same_file_creates_no_duplicates(
    conn: asyncpg.Connection, world: World
) -> None:
    storage = FakeStorage()
    await run(conn, storage, await upload(conn, world, storage))
    await run(conn, storage, await upload(conn, world, storage))
    counts = await conn.fetchrow(
        "select (select count(*) from student_profiles where nisn = '0099887766') as students,"
        " (select count(*) from classes where school_id = $1 and name = '9C') as classes,"
        " (select count(*) from school_memberships m join profiles p on p.id = m.user_id"
        "   where m.school_id = $1 and p.contact_email = 'joko@smp.id') as teachers,"
        " (select count(*) from profiles where contact_email = 'ibu.budi@mail.id') as parents",
        world.school_id,
    )
    assert counts is not None
    assert dict(counts) == {"students": 1, "classes": 1, "teachers": 1, "parents": 1}


async def test_known_teacher_from_another_school_gains_a_membership(
    conn: asyncpg.Connection, world: World
) -> None:
    other = await build_world(conn, "SMP Asal")
    teacher = await create_teacher(conn, other.school_id)
    email = await conn.fetchval("select contact_email from profiles where id = $1", teacher)
    storage = FakeStorage()
    text = f"{HEADER}\nteacher,Guru Pindahan,{email},,,,,,\n"
    await run(conn, storage, await upload(conn, world, storage, text))
    assert (
        await conn.fetchval(
            "select count(*) from school_memberships where user_id = $1 and role = 'teacher'",
            teacher,
        )
        == 2
    )
    assert await conn.fetchval("select count(*) from profiles where contact_email = $1", email) == 1


async def test_only_a_school_admin_uploads(conn: asyncpg.Connection, world: World) -> None:
    async with api_client(conn) as api:
        response = await api.post(
            f"/schools/{world.school_id}/roster-imports",
            data={"academic_year_id": str(world.year_id)},
            files={"file": ("roster.csv", ROSTER.encode(), "text/csv")},
            headers=as_user(world.teacher_id),
        )
    assert response.status_code == 404


class InterruptedAuthAdmin(FakeAuthAdmin):
    interrupted = False

    async def create_or_find(self, email: str, full_name: str) -> UUID:
        if email == "joko@smp.id" and not self.interrupted:
            self.interrupted = True
            raise AuthAdminError(retryable=True)
        return await super().create_or_find(email, full_name)


async def test_auth_failure_resumes_without_duplicate_students_or_errors(
    conn: asyncpg.Connection,
    world: World,
) -> None:
    storage = FakeStorage()
    body = await upload(conn, world, storage)
    handler = ImportRosterHandler(
        uow_on(conn), storage, InterruptedAuthAdmin(conn), FakeClock(), LIMITS
    )
    with pytest.raises(AuthAdminError):
        await handler.execute(UUID(body["import_id"]), UUID(body["job_id"]))
    assert (
        await conn.fetchval("select status::text from jobs where id = $1", UUID(body["job_id"]))
        == "queued"
    )
    await handler.execute(UUID(body["import_id"]), UUID(body["job_id"]))
    await handler.execute(UUID(body["import_id"]), UUID(body["job_id"]))
    assert (
        await conn.fetchval("select count(*) from student_profiles where nisn = '0099887766'") == 1
    )
    assert (
        await conn.fetchval(
            "select count(*) from roster_import_errors where import_id = $1",
            UUID(body["import_id"]),
        )
        == 1
    )


async def test_import_claim_excludes_live_workers_and_fences_replaced_attempts(
    conn: asyncpg.Connection,
    world: World,
) -> None:
    from datetime import timedelta

    body = await upload(conn, world, FakeStorage())
    iid, jid = UUID(body["import_id"]), UUID(body["job_id"])
    repo = PgRosterRepo(conn)
    now = FakeClock().now()
    stale = now - timedelta(seconds=LIMITS.stale_after_s)
    first = await repo.claim(iid, jid, now, stale)
    assert first == 1
    assert await repo.claim(iid, jid, now, stale) is None
    later = now + timedelta(seconds=LIMITS.stale_after_s + 1)
    assert await repo.claim(iid, jid, later, later - timedelta(seconds=LIMITS.stale_after_s)) == 2
    assert not await repo.keepalive(iid, jid, first, later)
    await repo.release_claim(iid, jid, first)
    assert await conn.fetchval("select status::text from jobs where id = $1", jid) == "running"


async def test_revoked_admin_cannot_run_queued_import(
    conn: asyncpg.Connection, world: World
) -> None:
    storage = FakeStorage()
    body = await upload(conn, world, storage)
    await conn.execute(
        "update school_memberships set status = 'inactive' where user_id = $1", world.admin_id
    )
    await run(conn, storage, body)
    assert (
        await conn.fetchval("select status::text from jobs where id = $1", UUID(body["job_id"]))
        == "failed"
    )
    assert (
        await conn.fetchval("select count(*) from student_profiles where nisn = '0099887766'") == 0
    )
    async with api_client(conn) as api:
        assert (
            await api.get(f"/jobs/{body['job_id']}", headers=as_user(world.admin_id))
        ).status_code == 404


async def test_job_cannot_import_another_jobs_roster(
    conn: asyncpg.Connection, world: World
) -> None:
    storage = FakeStorage()
    first = await upload(conn, world, storage)
    second = await upload(conn, world, storage)
    with pytest.raises(ImportInterrupted):
        await run(conn, storage, {"import_id": first["import_id"], "job_id": second["job_id"]})
    assert (
        await conn.fetchval("select status::text from jobs where id = $1", UUID(second["job_id"]))
        == "queued"
    )


async def test_invalid_header_marks_import_failed(conn: asyncpg.Connection, world: World) -> None:
    storage = FakeStorage()
    body = await upload(conn, world, storage, "role,full_name\nstudent,A\n")
    await run(conn, storage, body)
    view = await PgRosterRepo(conn).import_view(UUID(body["import_id"]))
    assert view is not None
    assert (view.status, view.rows_total, view.rows_succeeded, view.rows_failed) == (
        "failed",
        0,
        0,
        0,
    )
    assert view.errors[0].field == "header"


class CrashedAuthAdmin(FakeAuthAdmin):
    crashed = False

    async def create_or_find(self, email: str, full_name: str) -> UUID:
        user_id = await super().create_or_find(email, full_name)
        if not self.crashed:
            self.crashed = True
            raise SystemExit("process stopped after account creation")
        return user_id


async def test_process_death_after_auth_acceptance_resumes_with_the_same_account(
    conn: asyncpg.Connection,
    world: World,
) -> None:
    storage = FakeStorage()
    body = await upload(conn, world, storage)
    clock = FakeClock()
    handler = ImportRosterHandler(uow_on(conn), storage, CrashedAuthAdmin(conn), clock, LIMITS)
    with pytest.raises(SystemExit):
        await handler.execute(UUID(body["import_id"]), UUID(body["job_id"]))
    clock.advance(seconds=LIMITS.stale_after_s + 1)
    await handler.execute(UUID(body["import_id"]), UUID(body["job_id"]))
    assert (
        await conn.fetchval(
            "select count(*) from auth.users where email = '0099887766@siswa.nalar.id'"
        )
        == 1
    )
    assert (
        await conn.fetchval("select count(*) from student_profiles where nisn = '0099887766'") == 1
    )


@pytest.mark.parametrize(
    "data,code",
    [(b"\xff", "FILE_NOT_UTF8"), (b"x" * (LIMITS.max_bytes + 1), "FILE_TOO_LARGE")],
    ids=["not_utf8", "too_large"],
)
async def test_upload_rejects_unreadable_files_before_storage(
    conn: asyncpg.Connection,
    world: World,
    data: bytes,
    code: str,
) -> None:
    storage = FakeStorage()
    async with api_client(conn, storage=storage) as api:
        response = await api.post(
            f"/schools/{world.school_id}/roster-imports",
            data={"academic_year_id": str(world.year_id)},
            files={"file": ("roster.csv", data, "text/csv")},
            headers=as_user(world.admin_id),
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == code
    assert storage.objects == {}
