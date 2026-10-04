from uuid import UUID, uuid4

import asyncpg
from dishka import Provider, Scope, provide

from nalar.application.ports.auth_admin import AuthAccount, AuthAdmin
from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import World, build_world, create_class, create_student
from tests.integration.test_roster import FakeAuthAdmin


class PersonAuthAdmin(FakeAuthAdmin):
    def __init__(self, conn: asyncpg.Connection) -> None:
        super().__init__(conn)
        self.calls = 0

    async def create_or_find(self, email: str, full_name: str) -> AuthAccount:
        self.calls += 1
        account = await super().create_or_find(email, full_name)
        return AuthAccount(account.id, account.last_sign_in_at, email)


class PersonAdminAdapter(Provider):
    def __init__(self, admin: PersonAuthAdmin) -> None:
        super().__init__()
        self._admin = admin

    @provide(scope=Scope.APP)
    def admin(self) -> AuthAdmin:
        return self._admin


async def test_bulk_placement_rejects_foreign_students_without_partial_moves(
    conn: asyncpg.Connection, world: World
) -> None:
    foreign = await build_world(conn, "Other school")
    target = await create_class(conn, world.school_id, world.year_id, "8B")
    url = f"/schools/{world.school_id}/classes/{target}/students"
    async with api_client(conn) as api:
        rejected = await api.post(
            url,
            headers=as_user(world.admin_id),
            json={
                "user_ids": [str(world.student_id), str(foreign.student_id)],
            },
        )
        assert rejected.status_code == 404
        assert (
            await conn.fetchval(
                "select class_id from class_enrollments where student_id = $1"
                " and status = 'active'",
                world.student_id,
            )
            == world.class_id
        )
        body = {"user_ids": [str(world.student_id), str(world.student_id)]}
        for _ in range(2):
            placed = await api.post(url, headers=as_user(world.admin_id), json=body)
            assert placed.status_code == 204, placed.text
        assert (
            await conn.fetchval(
                "select count(*) from class_enrollments where student_id = $1"
                " and status = 'active'",
                world.student_id,
            )
            == 1
        )
        assert (
            await conn.fetchval(
                "select class_id from class_enrollments where student_id = $1"
                " and status = 'active'",
                world.student_id,
            )
            == target
        )


async def test_reactivation_restores_enrollment_and_preserves_explicit_parent_unlinks(
    conn: asyncpg.Connection, world: World
) -> None:
    second = await create_student(conn, world.school_id, world.class_id, world.year_id)
    root = f"/schools/{world.school_id}/people"
    async with api_client(conn) as api:
        headers = as_user(world.admin_id)
        link = f"{root}/{world.parent_id}/children/{second}"
        assert (
            await api.put(link, headers=headers, json={"relationship": "wali"})
        ).status_code == 204
        assert (await api.delete(link, headers=headers)).status_code == 204
        for person in (world.parent_id, world.student_id):
            assert (
                await api.post(f"{root}/{person}/deactivate", headers=headers)
            ).status_code == 204
        for person in (world.student_id, world.parent_id):
            response = await api.post(f"{root}/{person}/reactivate", headers=headers)
            assert response.status_code == 204, response.text
            assert (
                await api.post(f"{root}/{person}/reactivate", headers=headers)
            ).status_code == 204
        assert (
            await conn.fetchval(
                "select count(*) from class_enrollments where student_id = $1"
                " and status = 'active'",
                world.student_id,
            )
            == 1
        )
        assert await conn.fetchval(
            "select deactivated_at is not null from parent_student_links"
            " where parent_id = $1 and student_id = $2",
            world.parent_id,
            second,
        )
        assert await conn.fetchval(
            "select deactivated_at is null from parent_student_links"
            " where parent_id = $1 and student_id = $2",
            world.parent_id,
            world.student_id,
        )
        assert (
            await api.post(
                f"{root}/{world.student_id}/reactivate", headers=as_user(world.teacher_id)
            )
        ).status_code == 404


async def test_person_creation_scopes_before_auth_and_replays_without_duplicate_accounts(
    conn: asyncpg.Connection, world: World
) -> None:
    foreign = await build_world(conn, "Other school")
    fake = PersonAuthAdmin(conn)
    root = f"/schools/{world.school_id}/people"
    body = {
        "full_name": "New student",
        "role": "student",
        "nisn": str(int(uuid4().hex[:10], 16) % 10**10).zfill(10),
        "class_id": str(world.class_id),
    }
    headers = as_user(world.admin_id) | {"Idempotency-Key": str(uuid4())}
    async with api_client(conn, providers=(PersonAdminAdapter(fake),)) as api:
        rejected = await api.post(
            root, headers=headers, json=body | {"class_id": str(foreign.class_id)}
        )
        assert rejected.status_code == 404 and fake.calls == 0
        first = await api.post(root, headers=headers, json=body)
        assert first.status_code == 201, first.text
        repeated = await api.post(root, headers=headers, json=body)
        assert repeated.json() == first.json() and fake.calls == 1
        assert (
            await api.post(root, headers=headers, json=body | {"full_name": "Other name"})
        ).status_code == 409
        user_id = UUID(first.json()["user_id"])
        assert (
            await conn.fetchval(
                "select count(*) from class_enrollments where student_id = $1"
                " and status = 'active'",
                user_id,
            )
            == 1
        )
        assert (
            await conn.fetchval(
                "select count(*) from account_activations where user_id = $1",
                user_id,
            )
            == 0
        )


async def test_parent_links_require_both_people_in_the_admin_school(
    conn: asyncpg.Connection, world: World
) -> None:
    foreign = await build_world(conn, "Other school")
    root = f"/schools/{world.school_id}/people"
    async with api_client(conn) as api:
        for parent, student in (
            (world.parent_id, foreign.student_id),
            (foreign.parent_id, world.student_id),
        ):
            response = await api.put(
                f"{root}/{parent}/children/{student}", headers=as_user(world.admin_id), json={}
            )
            assert response.status_code == 404


async def test_parent_creation_links_only_local_children_and_queues_one_invitation(
    conn: asyncpg.Connection, world: World
) -> None:
    fake = PersonAuthAdmin(conn)
    body = {
        "full_name": "New parent",
        "role": "parent",
        "email": f"{uuid4()}@example.test",
        "child_ids": [str(world.student_id)],
        "relationship": "wali",
        "invite": True,
    }
    headers = as_user(world.admin_id) | {"Idempotency-Key": str(uuid4())}
    async with api_client(conn, providers=(PersonAdminAdapter(fake),)) as api:
        first = await api.post(f"/schools/{world.school_id}/people", json=body, headers=headers)
        assert first.status_code == 201, first.text
        repeated = await api.post(f"/schools/{world.school_id}/people", json=body, headers=headers)
        assert repeated.json() == first.json() and fake.calls == 1
        user_id = UUID(first.json()["user_id"])
        assert (
            await conn.fetchval(
                "select count(*) from parent_student_links where school_id = $1"
                " and parent_id = $2 and student_id = $3 and deactivated_at is null",
                world.school_id,
                user_id,
                world.student_id,
            )
            == 1
        )
        assert (
            await conn.fetchval(
                "select count(*) from notifications where school_id = $1 and recipient_id = $2"
                " and type = 'account_invitation'",
                world.school_id,
                user_id,
            )
            == 1
        )
