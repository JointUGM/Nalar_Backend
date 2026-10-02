import asyncio
import json
from datetime import timedelta
from uuid import UUID, uuid4

import asyncpg
import pytest
from dishka import Provider, Scope, provide

from nalar.application.ports.auth_admin import AuthAccount, AuthAdmin, AuthAdminError
from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import World, build_world
from tests.integration.test_account_invitations import queue, state
from tests.integration.test_roster import FakeAuthAdmin as RosterAuthAdmin
from tests.unit.application.fakes import FakeClock


class FakeAuthAdmin(RosterAuthAdmin):
    def __init__(self, conn: asyncpg.Connection) -> None:
        super().__init__(conn)
        self._lock = asyncio.Lock()

    async def get_account(self, user_id: UUID) -> AuthAccount | None:
        async with self._lock:
            return await super().get_account(user_id)


class AuthProvider(Provider):
    def __init__(self, auth: AuthAdmin) -> None:
        super().__init__()
        self._auth = auth

    @provide(scope=Scope.APP)
    def auth(self) -> AuthAdmin:
        return self._auth


async def test_bulk_is_atomic_scoped_and_reuses_existing_generation(
    conn: asyncpg.Connection, world: World
) -> None:
    other = await build_world(conn, "Other school")
    auth = FakeAuthAdmin(conn)
    await conn.execute("update profiles set has_real_email=true where id=$1", world.teacher_id)
    path = f"/schools/{world.school_id}/account-invitations"
    async with api_client(conn, providers=(AuthProvider(auth),)) as api:
        forbidden = await api.post(
            path, json={"user_ids": [str(world.teacher_id)]}, headers=as_user(world.teacher_id)
        )
        mixed = await api.post(
            path,
            json={"user_ids": [str(world.teacher_id), str(other.teacher_id)]},
            headers=as_user(world.admin_id),
        )
        assert forbidden.status_code == mixed.status_code == 404
        assert (
            await conn.fetchval(
                "select count(*) from account_activations where user_id=$1", world.teacher_id
            )
            == 0
        )
        first = await api.post(
            path, json={"user_ids": [str(world.teacher_id)]}, headers=as_user(world.admin_id)
        )
        repeated = await api.post(
            path,
            json={"user_ids": [str(world.teacher_id), str(world.teacher_id)]},
            headers=as_user(world.admin_id),
        )
    assert first.status_code == repeated.status_code == 202, first.text
    assert first.json()["queued"] == 1
    assert repeated.json()["queued"] == 0 and repeated.json()["skipped"] == 1
    assert first.json()["notification_ids"] == repeated.json()["notification_ids"]
    assert await conn.fetchval(
        "select onboarding_required from profiles where id=$1", world.teacher_id
    )
    audit = await conn.fetchval(
        "select changes from audit_logs where action='account_invitation.request' and entity_id=$1",
        world.teacher_id,
    )
    assert set(json.loads(audit)) == {"notification_id", "resend", "reason"}


async def test_active_and_synthetic_accounts_are_skipped(
    conn: asyncpg.Connection, world: World
) -> None:
    await conn.execute("update profiles set has_real_email=true where id=$1", world.teacher_id)
    await conn.execute("update auth.users set last_sign_in_at=now() where id=$1", world.teacher_id)
    await conn.execute("update profiles set has_real_email=false where id=$1", world.student_id)
    async with api_client(conn, providers=(AuthProvider(FakeAuthAdmin(conn)),)) as api:
        response = await api.post(
            f"/schools/{world.school_id}/account-invitations",
            json={"user_ids": [str(world.teacher_id), str(world.student_id)]},
            headers=as_user(world.admin_id),
        )
    assert response.status_code == 202, response.text
    assert response.json()["queued"] == 0
    assert {item["reason"] for item in response.json()["items"]} == {
        "already_active",
        "requires_assistance",
    }
    assert not await conn.fetchval(
        "select onboarding_required from profiles where id=$1", world.teacher_id
    )


async def test_lookup_outage_and_revocation_leave_no_admission(
    conn: asyncpg.Connection, world: World
) -> None:
    class Outage(FakeAuthAdmin):
        async def get_account(self, user_id: UUID) -> AuthAccount | None:
            raise AuthAdminError(retryable=True)

    await conn.execute("update profiles set has_real_email=true where id=$1", world.teacher_id)
    async with api_client(conn, providers=(AuthProvider(Outage(conn)),)) as api:
        response = await api.post(
            f"/schools/{world.school_id}/account-invitations",
            json={"user_ids": [str(world.teacher_id)]},
            headers=as_user(world.admin_id),
        )
    assert response.status_code == 503

    class Revocation(FakeAuthAdmin):
        async def get_account(self, user_id: UUID) -> AuthAccount | None:
            account = await super().get_account(user_id)
            await conn.execute(
                "update school_memberships set status='inactive' where user_id=$1", world.admin_id
            )
            return account

    async with api_client(conn, providers=(AuthProvider(Revocation(conn)),)) as api:
        response = await api.post(
            f"/schools/{world.school_id}/account-invitations",
            json={"user_ids": [str(world.teacher_id)]},
            headers=as_user(world.admin_id),
        )
    assert response.status_code == 404
    assert (
        await conn.fetchval(
            "select count(*) from account_activations where user_id=$1", world.teacher_id
        )
        == 0
    )


@pytest.mark.parametrize("phase", ["prepared", "submitting"])
async def test_resend_never_replaces_a_live_claim(
    conn: asyncpg.Connection, world: World, phase: str
) -> None:
    clock = FakeClock()
    notification_id = await queue(conn, world, clock)
    token = uuid4()
    await conn.execute(
        "update notifications set payload=payload || $2::jsonb where id=$1",
        notification_id,
        json.dumps(
            {
                "phase": phase,
                "token": str(token),
                "lease_until": (clock.now() + timedelta(minutes=3)).isoformat(),
            }
        ),
    )
    clock.advance(seconds=61)
    async with api_client(conn, clock=clock) as api:
        result = await api.post(
            f"/schools/{world.school_id}/account-invitations",
            json={"user_ids": [str(world.teacher_id)], "resend": True},
            headers=as_user(world.admin_id),
        )
    assert result.status_code == 202, result.text
    assert result.json()["queued"] == 0
    assert (await state(conn, notification_id))[1]["token"] == str(token)
    assert (
        await conn.fetchval(
            "select count(*) from account_activations where user_id=$1", world.teacher_id
        )
        == 1
    )


async def test_explicit_resend_supersedes_unknown_with_cooldown(
    conn: asyncpg.Connection, world: World
) -> None:
    clock = FakeClock()
    old = await queue(conn, world, clock)
    clock.advance(hours=5)
    await conn.execute(
        "update notifications set status='failed', payload=payload ||"
        ' \'{"outcome":"unknown","phase":"submitting"}\'::jsonb where id=$1',
        old,
    )
    await conn.execute(
        "update notifications set payload=payload || $2::jsonb where id=$1",
        old,
        json.dumps({"submission_times": [clock.now().isoformat()]}),
    )
    path = f"/schools/{world.school_id}/account-invitations"
    async with api_client(conn, clock=clock) as api:
        too_soon = await api.post(
            path,
            json={"user_ids": [str(world.teacher_id)], "resend": True},
            headers=as_user(world.admin_id),
        )
        assert too_soon.json()["items"][0]["reason"] == "cooldown"
        clock.advance(seconds=61)
        result = await api.post(
            path,
            json={"user_ids": [str(world.teacher_id)], "resend": True},
            headers=as_user(world.admin_id),
        )
    assert result.status_code == 202 and result.json()["queued"] == 1
    assert result.json()["notification_ids"] != [str(old)]
    assert (
        await conn.fetchval(
            "select count(*) from account_activations where user_id=$1"
            " and superseded_at is not null",
            world.teacher_id,
        )
        == 1
    )
    assert (await state(conn, old))[0] == "failed"


async def test_scheduled_retry_keeps_generation_and_attempts_until_queue_expiry(
    conn: asyncpg.Connection, world: World
) -> None:
    clock = FakeClock()
    old = await queue(conn, world, clock)
    await conn.execute(
        "update notifications set payload=payload || '{\"attempts\":2}'::jsonb where id=$1", old
    )
    await conn.execute(
        "update account_activations set expires_at=$2 where user_id=$1",
        world.teacher_id,
        clock.now() + timedelta(hours=1),
    )
    clock.advance(hours=2)
    path = f"/schools/{world.school_id}/account-invitations"
    async with api_client(conn, clock=clock) as api:
        repeated = await api.post(
            path,
            json={"user_ids": [str(world.teacher_id)], "resend": True},
            headers=as_user(world.admin_id),
        )
        assert repeated.json()["queued"] == 0
        assert repeated.json()["notification_ids"] == [str(old)]
        listing = await api.get(path, headers=as_user(world.admin_id))
        item = next(
            item for item in listing.json()["items"] if item["user_id"] == str(world.teacher_id)
        )
        assert item["state"] == "pending"
        clock.advance(days=2)
        await conn.execute(
            "update account_activations set expires_at=$2 where user_id=$1",
            world.teacher_id,
            clock.now() + timedelta(hours=1),
        )
        listing = await api.get(path, headers=as_user(world.admin_id))
        assert listing.json()["counts"]["expired"] == 1
        ordinary = await api.post(
            path, json={"user_ids": [str(world.teacher_id)]}, headers=as_user(world.admin_id)
        )
        assert ordinary.json()["items"][0]["reason"] == "resend_required"
        resent = await api.post(
            path,
            json={"user_ids": [str(world.teacher_id)], "resend": True},
            headers=as_user(world.admin_id),
        )
        assert resent.json()["queued"] == 1
    assert (await state(conn, old))[1]["attempts"] == 2


async def test_shared_account_does_not_expose_other_school_notification(
    conn: asyncpg.Connection, world: World
) -> None:
    other = await build_world(conn, "Other school")
    await conn.execute(
        "insert into school_memberships(school_id,user_id,role,status)"
        " values($1,$2,'teacher','active')",
        other.school_id,
        world.teacher_id,
    )
    clock = FakeClock()
    hidden = await queue(conn, world, clock)
    async with api_client(conn, clock=clock) as api:
        result = await api.post(
            f"/schools/{other.school_id}/account-invitations",
            json={"user_ids": [str(world.teacher_id)], "resend": True},
            headers=as_user(other.admin_id),
        )
        listing = await api.get(
            f"/schools/{other.school_id}/account-invitations", headers=as_user(other.admin_id)
        )
    assert result.status_code == 202 and listing.status_code == 200
    assert str(hidden) not in result.text + listing.text
    assert result.json()["notification_ids"] == []


async def test_status_paginates_scoped_sql_counts_without_payload_secrets(
    conn: asyncpg.Connection, world: World
) -> None:
    clock = FakeClock()
    notification_id = await queue(conn, world, clock)
    await conn.execute(
        "update notifications set payload=payload ||"
        ' \'{"token":"hidden-fence","category":"uncontrolled-secret"}\'::jsonb'
        " where id=$1",
        notification_id,
    )
    path = f"/schools/{world.school_id}/account-invitations"
    async with api_client(conn, clock=clock) as api:
        assert (await api.get(path, headers=as_user(world.teacher_id))).status_code == 404
        first = await api.get(path + "?limit=2", headers=as_user(world.admin_id))
        cursor = first.json()["next_cursor"]
        second = await api.get(path + f"?limit=2&cursor={cursor}", headers=as_user(world.admin_id))
    assert first.status_code == second.status_code == 200
    body = first.json()
    assert body["total"] == sum(body["counts"].values()) == 4
    assert body["counts"]["pending"] == 1
    ids = [item["user_id"] for item in body["items"] + second.json()["items"]]
    assert len(ids) == len(set(ids)) == 4
    assert "hidden-fence" not in first.text + second.text
    assert "uncontrolled-secret" not in first.text + second.text


async def test_bulk_requires_csrf_and_bounded_recipient_list(
    conn: asyncpg.Connection, world: World
) -> None:
    path = f"/schools/{world.school_id}/account-invitations"
    async with api_client(conn) as api:
        invalid = await api.post(path, json={"user_ids": []}, headers=as_user(world.admin_id))
        assert invalid.status_code == 400
        invalid = await api.post(
            path, json={"user_ids": [str(uuid4())] * 101}, headers=as_user(world.admin_id)
        )
        assert invalid.status_code == 400
        rejected = await api.post(
            path,
            json={"user_ids": [str(world.teacher_id)]},
            headers={**as_user(world.admin_id), "Origin": "https://attacker.test"},
        )
        assert rejected.status_code == 403
