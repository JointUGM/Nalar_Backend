import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from uuid import UUID

import asyncpg
import fakeredis
import pytest
from dishka import Provider, Scope, provide

from nalar.application.errors import DependencyUnavailable
from nalar.application.features.auth.commands.request_password_reset import PasswordResetPolicy
from nalar.application.features.auth.commands.send_password_reset import SendPasswordResetHandler
from nalar.application.features.onboarding.commands.send_invitation import (
    SendAccountInvitationHandler,
)
from nalar.application.ports.auth import (
    BrowserSessions,
    IdentityProvider,
    RecoveryProof,
)
from nalar.application.ports.auth_admin import AuthAdmin, AuthEmailError
from nalar.application.ports.password_resets import CredentialState
from nalar.infrastructure.auth.redis_sessions import RedisBrowserSessions
from nalar.infrastructure.db.repositories.password_resets import credential_snapshot
from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import World, create_user
from tests.integration.support.uow import uow_on
from tests.integration.test_account_activation import RecoveryIdentity
from tests.integration.test_account_invitations import TIMING, FakeAuth, queue
from tests.unit.application.fakes import FakeClock

POLICY = PasswordResetPolicy(
    True, "https://nalar.test/reset-password", timedelta(days=2), timedelta(minutes=1)
)
PASSWORD = "new-credential-password"


class ResetAdapters(Provider):
    def __init__(self, admin: FakeAuth) -> None:
        super().__init__()
        self._admin = admin

    @provide(scope=Scope.APP)
    def policy(self) -> PasswordResetPolicy:
        return POLICY

    @provide(scope=Scope.APP)
    def admin(self) -> AuthAdmin:
        return self._admin


class PasswordIdentity(RecoveryIdentity):
    def __init__(
        self, conn: asyncpg.Connection, user_id: UUID, email: str, token_hash: str
    ) -> None:
        super().__init__(user_id, email, token_hash)
        self.conn = conn
        self.global_error = False
        self.global_revocations = 0

    async def update_password(self, proof: RecoveryProof, password: str) -> None:
        await super().update_password(proof, password)
        await self.conn.execute(
            "update auth.users set encrypted_password=$2 where id=$1",
            proof.user_id,
            hashlib.sha256(password.encode()).hexdigest(),
        )

    async def sign_out_all(self, access_token: str) -> None:
        if self.global_error:
            raise DependencyUnavailable()
        self.global_revocations += 1
        await super().sign_out_all(access_token)


async def recipient_email(conn: asyncpg.Connection, user_id: UUID) -> str:
    await conn.execute(
        (
            "update auth.users set "
            "email_confirmed_at=now(),encrypted_password='old-digest' where "
            "id=$1"
        ),
        user_id,
    )
    email: str = await conn.fetchval("select email from auth.users where id=$1", user_id)
    return email


async def issued_reset(
    conn: asyncpg.Connection, world: World, clock: FakeClock
) -> tuple[UUID, dict[str, str], PasswordIdentity]:
    email = await recipient_email(conn, world.teacher_id)
    async with uow_on(conn) as uow:
        reset_id = await uow.password_resets.request(
            email, clock.now(), clock.now() + timedelta(days=2), timedelta(minutes=1)
        )
    assert reset_id is not None
    await SendPasswordResetHandler(uow_on(conn), clock, FakeAuth(conn), TIMING, POLICY).execute(
        reset_id
    )
    token: str = await conn.fetchval(
        "select recovery_token from auth.users where id=$1", world.teacher_id
    )
    identity = PasswordIdentity(conn, world.teacher_id, email, token)
    return (
        reset_id,
        {"reset_id": str(reset_id), "token_hash": token, "password": PASSWORD},
        identity,
    )


async def test_request_is_silent_for_unknown_throttled_and_disabled_addresses(
    conn: asyncpg.Connection, world: World
) -> None:
    email = await recipient_email(conn, world.teacher_id)
    admin = FakeAuth(conn)
    async with api_client(conn, providers=(ResetAdapters(admin),)) as api:
        for address in ["unknown@test.nalar", email] + [email] * 11:
            response = await api.post("/auth/password-reset", json={"email": address})
            assert response.status_code == 202 and response.content == b""
    assert (
        await conn.fetchval(
            "select count(*) from password_resets where user_id=$1", world.teacher_id
        )
        == 1
    )
    assert admin.calls == []
    other_email = await recipient_email(conn, world.student_id)
    async with api_client(conn) as api:
        assert (
            await api.post("/auth/password-reset", json={"email": other_email})
        ).status_code == 202
    assert not await conn.fetchval(
        "select exists(select 1 from password_resets where user_id=$1)", world.student_id
    )


@pytest.mark.parametrize(
    "ineligible", ["synthetic", "onboarding", "unverified", "inactive", "suspended"]
)
async def test_ineligible_account_never_queues_reset(
    conn: asyncpg.Connection, world: World, ineligible: str
) -> None:
    email = await recipient_email(conn, world.teacher_id)
    if ineligible == "synthetic":
        await conn.execute("update profiles set has_real_email=false where id=$1", world.teacher_id)
    elif ineligible == "onboarding":
        await conn.execute(
            "update profiles set onboarding_required=true where id=$1", world.teacher_id
        )
    elif ineligible == "unverified":
        await conn.execute(
            "update auth.users set email_confirmed_at=null where id=$1", world.teacher_id
        )
    elif ineligible == "inactive":
        await conn.execute(
            "update school_memberships set status='inactive' where user_id=$1", world.teacher_id
        )
    else:
        await conn.execute("update schools set is_active=false where id=$1", world.school_id)
    async with api_client(conn, providers=(ResetAdapters(FakeAuth(conn)),)) as api:
        assert (await api.post("/auth/password-reset", json={"email": email})).status_code == 202
    assert not await conn.fetchval(
        "select exists(select 1 from password_resets where user_id=$1)", world.teacher_id
    )


@pytest.mark.parametrize("role", ["parent", "platform"])
async def test_recovery_uses_parent_and_platform_scope_without_school_membership(
    conn: asyncpg.Connection, world: World, role: str
) -> None:
    user_id = await create_user(conn)
    email = await recipient_email(conn, user_id)
    if role == "parent":
        await conn.execute(
            "insert into parent_student_links(parent_id,student_id,school_id) values($1,$2,$3)",
            user_id,
            world.student_id,
            world.school_id,
        )
    else:
        await conn.execute("update profiles set is_platform_admin=true where id=$1", user_id)
    async with uow_on(conn) as uow:
        assert await uow.password_resets.request(
            email, FakeClock().now(), FakeClock().now() + timedelta(days=2), timedelta(minutes=1)
        )


async def test_duplicate_delivery_binds_one_proof_without_changing_onboarding(
    conn: asyncpg.Connection, world: World
) -> None:
    clock = FakeClock()
    email = await recipient_email(conn, world.teacher_id)
    admin = FakeAuth(conn)
    async with uow_on(conn) as uow:
        reset_id = await uow.password_resets.request(
            email, clock.now(), clock.now() + timedelta(days=2), POLICY.cooldown
        )
    assert reset_id is not None
    handler = SendPasswordResetHandler(uow_on(conn), clock, admin, TIMING, POLICY)
    await handler.execute(reset_id)
    await handler.execute(reset_id)
    row = await conn.fetchrow(
        "select status,proof_digest from password_resets where id=$1", reset_id
    )
    assert row is not None and row["status"] == "sent" and row["proof_digest"]
    assert len(admin.calls) == 1 and f"reset_id={reset_id}" in admin.calls[0][1]
    assert not await conn.fetchval(
        "select onboarding_required from profiles where id=$1", world.teacher_id
    )


async def test_uncertain_email_acceptance_is_terminal_for_automatic_retries(
    conn: asyncpg.Connection, world: World
) -> None:
    clock = FakeClock()
    email = await recipient_email(conn, world.teacher_id)
    async with uow_on(conn) as uow:
        reset_id = await uow.password_resets.request(
            email, clock.now(), clock.now() + timedelta(days=2), POLICY.cooldown
        )
    assert reset_id is not None
    admin = FakeAuth(conn)
    admin.error = AuthEmailError("transport", acceptance_unknown=True)
    handler = SendPasswordResetHandler(uow_on(conn), clock, admin, TIMING, POLICY)
    await handler.execute(reset_id)
    clock.advance(hours=1)
    await handler.dispatch_due()
    await handler.execute(reset_id)
    assert len(admin.calls) == 1
    assert (
        await conn.fetchval("select status from password_resets where id=$1", reset_id) == "failed"
    )


async def test_reset_confirmation_changes_password_once_and_revokes_all_cookies(
    conn: asyncpg.Connection, world: World
) -> None:
    clock = FakeClock()
    reset_id, body, identity = await issued_reset(conn, world, clock)
    async with api_client(conn, clock=clock, identity=identity) as api:
        await api.post("/auth/login", json={"email": identity.email, "password": identity.password})
        cookie = api.cookies.get("nalar_session")
        result = await api.post("/auth/password-reset/confirm", json=body)
        assert result.status_code == 204, result.text
        assert result.content == b"" and api.cookies.get("nalar_session") is None
        assert (
            await api.get("/auth/session", headers={"Cookie": f"nalar_session={cookie}"})
        ).status_code == 401
        replay = await api.post("/auth/password-reset/confirm", json=body)
        assert replay.status_code == 400 and replay.json()["error"]["code"] == "INVALID_ACTIVATION"
    assert identity.updated == [PASSWORD] and identity.global_revocations == 1
    assert await conn.fetchval("select consumed_at from password_resets where id=$1", reset_id)
    assert not (await credential_snapshot(conn, world.teacher_id)).blocked


async def test_weak_password_does_not_consume_reset_proof(
    conn: asyncpg.Connection, world: World
) -> None:
    clock = FakeClock()
    _, body, identity = await issued_reset(conn, world, clock)
    async with api_client(conn, clock=clock, identity=identity) as api:
        weak = await api.post("/auth/password-reset/confirm", json={**body, "password": "short"})
        assert weak.status_code == 400 and weak.json()["error"]["code"] == "WEAK_PASSWORD"
        assert identity.verifications == 0
        assert (await api.post("/auth/password-reset/confirm", json=body)).status_code == 204


@pytest.mark.parametrize("invalid", ["expired", "wrong_hash", "wrong_subject"])
async def test_invalid_reset_cannot_update_credentials(
    conn: asyncpg.Connection, world: World, invalid: str
) -> None:
    clock = FakeClock()
    _, body, identity = await issued_reset(conn, world, clock)
    if invalid == "expired":
        clock.advance(hours=2)
    elif invalid == "wrong_hash":
        body["token_hash"] = "incorrect-proof"
    else:
        identity.user_id = world.student_id
    async with api_client(conn, clock=clock, identity=identity) as api:
        result = await api.post("/auth/password-reset/confirm", json=body)
    assert result.status_code == 400 and result.json()["error"]["code"] == "INVALID_ACTIVATION"
    assert identity.updated == []
    assert not (await credential_snapshot(conn, world.teacher_id)).blocked


async def test_signed_in_change_requires_current_password_and_active_caller(
    conn: asyncpg.Connection, world: World
) -> None:
    email = await recipient_email(conn, world.teacher_id)
    identity = PasswordIdentity(conn, world.teacher_id, email, "")
    async with api_client(conn, identity=identity) as api:
        await api.post("/auth/login", json={"email": email, "password": identity.password})
        wrong = await api.post(
            "/auth/password", json={"current_password": "wrong", "new_password": PASSWORD}
        )
        assert wrong.status_code == 401 and identity.updated == []
        cookie = api.cookies.get("nalar_session")
        changed = await api.post(
            "/auth/password", json={"current_password": identity.password, "new_password": PASSWORD}
        )
        assert changed.status_code == 204, changed.text
        assert (
            await api.get("/auth/session", headers={"Cookie": f"nalar_session={cookie}"})
        ).status_code == 401
        await conn.execute(
            "update school_memberships set status='inactive' where user_id=$1", world.student_id
        )
        inactive = await api.post(
            "/auth/password",
            headers=as_user(world.student_id),
            json={"current_password": identity.password, "new_password": PASSWORD},
        )
        assert inactive.status_code == 404
    assert identity.updated == [PASSWORD]


async def test_failed_revocation_blocks_sessions_until_changed_password_login_reconciles(
    conn: asyncpg.Connection, world: World
) -> None:
    clock = FakeClock()
    reset_id, body, identity = await issued_reset(conn, world, clock)
    identity.global_error = True
    async with api_client(conn, clock=clock, identity=identity) as api:
        await api.post("/auth/login", json={"email": identity.email, "password": identity.password})
        cookie = api.cookies.get("nalar_session")
        assert (await api.post("/auth/password-reset/confirm", json=body)).status_code == 503
        assert (await credential_snapshot(conn, world.teacher_id)).blocked
        assert (
            await api.get("/auth/session", headers={"Cookie": f"nalar_session={cookie}"})
        ).status_code == 401
        identity.global_error = False
        clock.advance(minutes=2)
        login = await api.post("/auth/login", json={"email": identity.email, "password": PASSWORD})
        assert login.status_code == 200, login.text
        assert (await api.get("/auth/session")).status_code == 200
    assert not (await credential_snapshot(conn, world.teacher_id)).blocked
    assert (
        await conn.fetchval("select status from password_resets where id=$1", reset_id)
        == "consumed"
    )


async def test_reset_and_invitation_share_project_delivery_quota(
    conn: asyncpg.Connection, world: World
) -> None:
    clock = FakeClock()
    timing = replace(TIMING, hourly_limit=1)
    notification_id = await queue(conn, world, clock)
    admin = FakeAuth(conn)
    await SendAccountInvitationHandler(uow_on(conn), clock, admin, timing).execute(notification_id)
    email = await recipient_email(conn, world.student_id)
    async with uow_on(conn) as uow:
        reset_id = await uow.password_resets.request(
            email, clock.now(), clock.now() + timedelta(days=2), POLICY.cooldown
        )
    assert reset_id is not None
    await SendPasswordResetHandler(uow_on(conn), clock, admin, timing, POLICY).execute(reset_id)
    assert len(admin.calls) == 1
    row = await conn.fetchrow(
        "select status,scheduled_for,payload from password_resets where id=$1", reset_id
    )
    assert row is not None and row["status"] == "pending" and row["scheduled_for"] > clock.now()
    assert not json.loads(row["payload"]).get("submission_times")


async def test_durable_revision_invalidates_cookie_even_when_redis_revocation_fails(
    conn: asyncpg.Connection, world: World
) -> None:
    class UnavailableRevocation(RedisBrowserSessions):
        async def revoke_user(self, user_id: UUID) -> None:
            raise DependencyUnavailable()

    class SessionAdapters(Provider):
        @provide(scope=Scope.APP)
        def sessions(
            self, identity: IdentityProvider, credentials: CredentialState
        ) -> BrowserSessions:
            return UnavailableRevocation(
                fakeredis.FakeAsyncRedis(), identity, 43200, 60, 15, credentials=credentials
            )

    email = await recipient_email(conn, world.teacher_id)
    identity = PasswordIdentity(conn, world.teacher_id, email, "")
    async with api_client(conn, identity=identity, providers=(SessionAdapters(),)) as api:
        assert (
            await api.post("/auth/login", json={"email": email, "password": identity.password})
        ).status_code == 200
        cookie = api.cookies.get("nalar_session")
        failed = await api.post(
            "/auth/password", json={"current_password": identity.password, "new_password": PASSWORD}
        )
        assert failed.status_code == 503
        assert (
            await api.get("/auth/session", headers={"Cookie": f"nalar_session={cookie}"})
        ).status_code == 401
    assert identity.updated == []
    assert (await credential_snapshot(conn, world.teacher_id)).revision == 1
    assert not (await credential_snapshot(conn, world.teacher_id)).blocked
