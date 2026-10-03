import asyncio
from typing import Any
from uuid import UUID, uuid4

import asyncpg
from dishka import Provider, Scope, provide

from nalar.application.errors import NotFound
from nalar.application.features.administration.commands.deactivate_person import (
    DeactivatePersonHandler,
)
from nalar.application.features.onboarding.commands.reconcile_login import (
    ReconcileOnboardingHandler,
)
from nalar.application.ports.auth_admin import AuthAccount, AuthAdmin, AuthAdminError
from nalar.infrastructure.db.authz import PgAuthz
from nalar.infrastructure.db.uow import PgUnitOfWork
from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import (
    World,
    add_membership,
    build_world,
    create_class,
    create_teacher,
    create_user,
)
from tests.integration.support.uow import uow_on
from tests.integration.test_release import settled
from tests.integration.test_roster import FakeAuthAdmin
from tests.unit.application.fakes import FakeClock


class PlatformAuthAdmin(FakeAuthAdmin):
    async def create_or_find(self, email: str, full_name: str) -> AuthAccount:
        account = await super().create_or_find(email, full_name)
        return AuthAccount(account.id, account.last_sign_in_at, email)


class RetryPlatformAuthAdmin(PlatformAuthAdmin):
    fail_next = True

    async def create_or_find(self, email: str, full_name: str) -> AuthAccount:
        if self.fail_next:
            self.fail_next = False
            raise AuthAdminError(retryable=True)
        return await super().create_or_find(email, full_name)


class AdminAdapter(Provider):
    def __init__(self, admin: FakeAuthAdmin) -> None:
        super().__init__()
        self._admin = admin

    @provide(scope=Scope.APP)
    def admin(self) -> AuthAdmin:
        return self._admin


async def platform_user(conn: asyncpg.Connection) -> UUID:
    user_id = await create_user(conn, "Platform Admin")
    await conn.execute("update profiles set is_platform_admin = true where id = $1", user_id)
    return user_id


async def test_people_scope_placement_last_admin_and_idempotent_revocation(
    conn: asyncpg.Connection,
    world: World,
) -> None:
    foreign = await build_world(conn, "Other school")
    replacement = await create_user(conn, "Second Admin")
    await add_membership(conn, world.school_id, replacement, "school_admin")
    target_class = await create_class(conn, world.school_id, world.year_id, "8B")
    root = f"/schools/{world.school_id}"
    async with api_client(conn) as api:
        people = await api.get(root + "/people", headers=as_user(world.admin_id))
        assert people.status_code == 200, people.text
        assert all(p["user_id"] != str(foreign.student_id) for p in people.json()["items"])
        student = next(p for p in people.json()["items"] if p["user_id"] == str(world.student_id))
        assert "***@" in student["email"] and student["nisn"]
        forbidden = await api.patch(
            root + f"/people/{foreign.student_id}",
            json={"full_name": "Wrong"},
            headers=as_user(world.admin_id),
        )
        assert forbidden.status_code == 404
        moved = await api.patch(
            root + f"/people/{world.student_id}",
            json={"class_id": str(target_class)},
            headers=as_user(world.admin_id),
        )
        assert moved.status_code == 204, moved.text
        assert (
            await conn.fetchval(
                "select count(*) from class_enrollments"
                " where student_id = $1 and status = 'active'",
                world.student_id,
            )
            == 1
        )
        assert (
            await conn.fetchval("select count(*) from sessions where id = $1", world.session_id)
            == 1
        )
        deactivate = root + f"/people/{replacement}/deactivate"
        first = await api.post(deactivate, headers=as_user(world.admin_id))
        again = await api.post(deactivate, headers=as_user(world.admin_id))
        assert first.status_code == again.status_code == 204
        assert (
            await conn.fetchval(
                "select credential_revision from profiles where id = $1", replacement
            )
            == 1
        )
        last = await api.post(
            root + f"/people/{world.admin_id}/deactivate", headers=as_user(world.admin_id)
        )
        assert (last.status_code, last.json()["error"]["code"]) == (409, "LAST_SCHOOL_ADMIN")
        denied = await api.get(root + "/people", headers=as_user(replacement))
        assert denied.status_code == 401
        assert not await PgAuthz(conn).is_school_admin(replacement, world.school_id)


async def test_class_assignment_and_kb_owner_validate_school_and_preserve_versions(
    conn: asyncpg.Connection,
    world: World,
) -> None:
    foreign = await build_world(conn, "Other school")
    teacher = await create_teacher(conn, world.school_id)
    root = f"/schools/{world.school_id}"
    async with api_client(conn) as api:
        denied = await api.get(root + "/classes", headers=as_user(world.teacher_id))
        assert denied.status_code == 404
        body = {
            "name": "7C",
            "grade_level": 7,
            "academic_year_id": str(world.year_id),
            "homeroom_teacher_id": str(teacher),
        }
        wrong = await api.post(
            root + "/classes",
            json=body | {"homeroom_teacher_id": str(foreign.teacher_id)},
            headers=as_user(world.admin_id),
        )
        assert wrong.status_code == 404
        created = await api.post(root + "/classes", json=body, headers=as_user(world.admin_id))
        assert created.status_code == 201, created.text
        class_id = created.json()["class_id"]
        edited = await api.patch(
            root + f"/classes/{class_id}", json={"name": "7D"}, headers=as_user(world.admin_id)
        )
        assert edited.json()["homeroom_teacher_id"] == str(teacher)
        before = await conn.fetchval(
            "select context_pack from mission_versions where id = $1", world.version_id
        )
        assignment = {
            "class_id": str(world.class_id),
            "school_subject_id": str(world.subject_id),
            "teacher_id": str(teacher),
        }
        replaced = await api.put(
            root + "/assignments", json=assignment, headers=as_user(world.admin_id)
        )
        assert replaced.status_code == 204, replaced.text
        assert not await PgAuthz(conn).teaches_publication(world.teacher_id, world.publication_id)
        assert await PgAuthz(conn).teaches_publication(teacher, world.publication_id)
        transferred = await api.post(
            f"/knowledge-bases/{world.kb_id}/owner",
            json={"teacher_id": str(teacher)},
            headers=as_user(world.admin_id),
        )
        assert transferred.status_code == 204, transferred.text
        assert (
            await conn.fetchval(
                "select context_pack from mission_versions where id = $1", world.version_id
            )
            == before
        )
        assignments = await api.get(
            root + "/assignments",
            params={"academic_year_id": str(world.year_id)},
            headers=as_user(world.admin_id),
        )
        assert [a["teacher_id"] for a in assignments.json()] == [str(teacher)]
        removed = await api.put(
            root + "/assignments",
            json=assignment | {"teacher_id": None},
            headers=as_user(world.admin_id),
        )
        assert removed.status_code == 204


async def test_academic_rollover_retries_once_and_copies_only_classes(
    conn: asyncpg.Connection,
    world: World,
) -> None:
    foreign = await build_world(conn, "Other school")
    url = f"/schools/{world.school_id}/academic-years"
    headers = as_user(world.admin_id) | {"Idempotency-Key": str(uuid4())}
    body = {
        "name": "2027/2028",
        "starts_on": "2027-07-01",
        "ends_on": "2028-06-30",
        "copy_classes_from": str(world.year_id),
    }
    async with api_client(conn) as api:
        missing = await api.post(
            url, json=body | {"copy_classes_from": str(foreign.year_id)}, headers=headers
        )
        assert missing.status_code == 404
        first = await api.post(url, json=body, headers=headers)
        assert first.status_code == 201, first.text
        repeat = await api.post(url, json=body, headers=headers)
        assert repeat.json() == first.json()
        conflict = await api.post(url, json=body | {"name": "Other"}, headers=headers)
        assert (conflict.status_code, conflict.json()["error"]["code"]) == (
            409,
            "IDEMPOTENCY_CONFLICT",
        )
        year_id = UUID(first.json()["academic_year_id"])
        assert (
            await conn.fetchval("select count(*) from classes where academic_year_id = $1", year_id)
            == 1
        )
        assert (
            await conn.fetchval(
                "select count(*) from class_enrollments where academic_year_id = $1", year_id
            )
            == 0
        )
        assert (
            await conn.fetchval(
                "select count(*) from teaching_assignments ta join classes c on c.id = ta.class_id"
                " where c.academic_year_id = $1",
                year_id,
            )
            == 0
        )
        assert (
            await conn.fetchval(
                "select is_current from academic_years where id = $1", world.year_id
            )
            is False
        )


async def test_platform_curriculum_publication_is_atomic_and_mapping_scoped(
    conn: asyncpg.Connection,
    world: World,
) -> None:
    platform = await platform_user(conn)
    body: dict[str, Any] = {
        "name": "Published CP",
        "decree_code": f"TEST/{uuid4()}",
        "effective_on": "2026-10-01",
        "subjects": [
            {
                "name": "IPA",
                "phase": "D",
                "learning_outcomes": [
                    {
                        "description": "Peserta didik menjelaskan gaya dan gerak.",
                        "element": "Pemahaman IPA",
                    }
                ],
            }
        ],
    }
    headers = as_user(platform) | {"Idempotency-Key": str(uuid4())}
    async with api_client(conn) as api:
        denied = await api.get("/platform/curriculum-versions", headers=as_user(world.admin_id))
        assert denied.status_code == 404
        first = await api.post("/platform/curriculum-versions", json=body, headers=headers)
        assert first.status_code == 201, first.text
        repeat = await api.post("/platform/curriculum-versions", json=body, headers=headers)
        assert repeat.json() == first.json()
        version_id = first.json()["curriculum_version_id"]
        detail = await api.get(
            f"/platform/curriculum-versions/{version_id}", headers=as_user(platform)
        )
        assert detail.json()["is_current"] is True
        assert (
            detail.json()["subjects"][0]["learning_outcomes"][0]["description"]
            == body["subjects"][0]["learning_outcomes"][0]["description"]
        )
        mapped = await api.put(
            f"/schools/{world.school_id}/subjects/{world.subject_id}/curriculum",
            json={"cp_version_id": version_id},
            headers=as_user(world.admin_id),
        )
        assert mapped.status_code == 204, mapped.text
        subjects = await api.get(
            f"/schools/{world.school_id}/subjects", headers=as_user(world.admin_id)
        )
        assert subjects.json()[0]["cp_version_id"] == version_id
        duplicate = await api.post(
            "/platform/curriculum-versions",
            json=body,
            headers=as_user(platform) | {"Idempotency-Key": str(uuid4())},
        )
        assert duplicate.status_code == 409
        assert await conn.fetchval("select count(*) from cp_versions where is_current") == 1


async def test_platform_onboarding_handoff_and_suspension_preserve_scope(
    conn: asyncpg.Connection,
    world: World,
) -> None:
    await settled(conn, world)
    platform = await platform_user(conn)
    fake = RetryPlatformAuthAdmin(conn)
    provider = AdminAdapter(fake)
    root = f"/platform/schools/{world.school_id}"
    headers = as_user(platform) | {"Idempotency-Key": str(uuid4())}
    body = {
        "name": "New SMP",
        "npsn": f"{int(uuid4().hex[:8], 16) % 100000000:08d}",
        "city": "Bandung",
        "admin_email": f"{uuid4()}@test.nalar",
    }
    async with api_client(conn, providers=(provider,)) as api:
        failed = await api.post("/platform/schools", json=body, headers=headers)
        assert failed.status_code == 503
        assert (
            await conn.fetchval("select count(*) from schools where npsn = $1", body["npsn"]) == 0
        )
        assert (
            await conn.fetchval(
                "select count(*) from admin_requests where actor_id = $1"
                " and operation = 'school_onboard' and result_id is null",
                platform,
            )
            == 1
        )
        first = await api.post("/platform/schools", json=body, headers=headers)
        assert first.status_code == 201, first.text
        again = await api.post("/platform/schools", json=body, headers=headers)
        assert again.json() == first.json()
        new_school = UUID(first.json()["school_id"])
        assert (
            await conn.fetchval(
                "select count(*) from school_memberships where school_id = $1"
                " and role = 'school_admin' and status = 'active'",
                new_school,
            )
            == 1
        )
        assert (
            await conn.fetchval(
                "select count(*) from notifications where school_id = $1"
                " and type = 'account_invitation'",
                new_school,
            )
            == 1
        )
        email = await conn.fetchval(
            "select contact_email from profiles where id = $1", world.teacher_id
        )
        handoff = await api.post(root + "/admin", json={"email": email}, headers=headers)
        assert handoff.status_code == 200, handoff.text
        assert handoff.json()["user_id"] == str(world.teacher_id)
        assert not await PgAuthz(conn).is_school_admin(world.admin_id, world.school_id)
        pending = await api.post(
            root + "/admin",
            json={"email": f"{uuid4()}@test.nalar"},
            headers=as_user(platform) | {"Idempotency-Key": str(uuid4())},
        )
        assert pending.status_code == 200, pending.text
        assert pending.json()["pending_activation"] is True
        assert await PgAuthz(conn).is_school_admin(world.teacher_id, world.school_id)
        incumbent = await api.post(
            f"/schools/{world.school_id}/people/{world.teacher_id}/deactivate",
            headers=as_user(world.teacher_id),
        )
        assert (incumbent.status_code, incumbent.json()["error"]["code"]) == (
            409,
            "LAST_SCHOOL_ADMIN",
        )
        new_admin = UUID(pending.json()["user_id"])
        await ReconcileOnboardingHandler(uow_on(conn), FakeClock()).execute(new_admin)
        assert await PgAuthz(conn).is_school_admin(new_admin, world.school_id)
        assert not await PgAuthz(conn).is_school_admin(world.teacher_id, world.school_id)
        suspended = await api.patch(
            root + "/status", json={"status": "suspended"}, headers=as_user(platform)
        )
        assert suspended.status_code == 204
        authz = PgAuthz(conn)
        assert not await authz.teaches_session(world.teacher_id, world.session_id)
        assert not await authz.owns_session(world.student_id, world.session_id)
        assert not await authz.is_linked_parent(world.parent_id, world.student_id)
        assert (await api.get("/student/missions", headers=as_user(world.student_id))).json() == {
            "upcoming": [],
            "open": [],
            "completed": [],
        }
        assert (await api.get("/parent/children", headers=as_user(world.parent_id))).json()[
            "items"
        ] == []
        resumed = await api.patch(
            root + "/status", json={"status": "active"}, headers=as_user(platform)
        )
        assert resumed.status_code == 204
        assert await authz.teaches_session(world.teacher_id, world.session_id)
        schools = await api.get(
            "/platform/schools", params={"q": "New SMP"}, headers=as_user(platform)
        )
        assert schools.json()["items"][0]["city"] == "Bandung"
        assert schools.json()["counts"]["total"] >= 2


async def test_competing_admin_deactivations_cannot_remove_every_admin(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        world = await build_world(conn, f"Admin race {uuid4().hex[:8]}")
        other = await create_user(conn, "Second Admin")
        await add_membership(conn, world.school_id, other, "school_admin")
    results = await asyncio.gather(
        DeactivatePersonHandler(PgUnitOfWork(pool.acquire)).execute(
            world.admin_id, world.school_id, other
        ),
        DeactivatePersonHandler(PgUnitOfWork(pool.acquire)).execute(
            other, world.school_id, world.admin_id
        ),
        return_exceptions=True,
    )
    assert sum(result is None for result in results) == 1
    assert sum(isinstance(result, NotFound) for result in results) == 1
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "select count(*) from school_memberships where school_id = $1"
                " and role = 'school_admin' and status = 'active'",
                world.school_id,
            )
            == 1
        )
