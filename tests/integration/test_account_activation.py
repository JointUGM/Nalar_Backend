from collections.abc import Awaitable, Callable
from uuid import UUID, uuid4

import asyncpg
import pytest

from nalar.application.errors import DependencyUnavailable, InvalidActivation
from nalar.application.features.onboarding.commands.send_invitation import (
    SendAccountInvitationHandler,
)
from nalar.application.ports.auth import RecoveryProof
from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import World
from tests.integration.support.uow import uow_on
from tests.integration.test_account_invitations import TIMING, FakeAuth, queue, state
from tests.unit.application.fakes import FakeClock, FakeIdentityProvider

PASSWORD = "recipient-new-password"


class RecoveryIdentity(FakeIdentityProvider):
    def __init__(self, user_id: UUID, email: str, token_hash: str) -> None:
        super().__init__()
        self.user_id, self.email, self.token_hash = user_id, email, token_hash
        self.verifications = 0
        self.used = False
        self.updated: list[str] = []
        self.before_verify: Callable[[], Awaitable[None]] | None = None
        self.update_error = False

    async def verify_recovery(self, token_hash: str) -> RecoveryProof:
        self.verifications += 1
        if self.used or token_hash != self.token_hash:
            raise InvalidActivation()
        if self.before_verify:
            await self.before_verify()
        self.used = True
        return RecoveryProof(self.user_id, self.email, "temporary-access")

    async def update_password(self, proof: RecoveryProof, password: str) -> None:
        if self.update_error:
            raise DependencyUnavailable()
        self.password = password
        self.updated.append(password)


async def issued(
    conn: asyncpg.Connection, world: World, clock: FakeClock
) -> tuple[UUID, dict[str, str], RecoveryIdentity]:
    notification_id = await queue(conn, world, clock)
    await SendAccountInvitationHandler(uow_on(conn), clock, FakeAuth(conn), TIMING).execute(
        notification_id
    )
    _, payload = await state(conn, notification_id)
    row = await conn.fetchrow(
        "select email,recovery_token from auth.users where id=$1", world.teacher_id
    )
    assert row is not None
    body = {
        "activation_id": payload["activation_id"],
        "token_hash": row["recovery_token"],
        "password": PASSWORD,
    }
    return (
        notification_id,
        body,
        RecoveryIdentity(world.teacher_id, row["email"], row["recovery_token"]),
    )


async def test_activation_sets_password_once_without_issuing_browser_session(
    conn: asyncpg.Connection, world: World
) -> None:
    clock = FakeClock()
    _, body, identity = await issued(conn, world, clock)
    async with api_client(conn, clock=clock, identity=identity) as api:
        activated = await api.post("/auth/activate", json=body)
        assert activated.status_code == 204, activated.text
        assert activated.content == b"" and "Set-Cookie" not in activated.headers
        assert activated.headers["Cache-Control"] == "no-store"
        assert (await api.get("/auth/session")).status_code == 401
        replay = await api.post("/auth/activate", json=body)
        assert replay.status_code == 400 and replay.json()["error"]["code"] == "INVALID_ACTIVATION"
        login = await api.post("/auth/login", json={"email": identity.email, "password": PASSWORD})
        assert login.status_code == 200 and "Set-Cookie" in login.headers
    assert identity.updated == [PASSWORD] and identity.verifications == 1
    assert identity.revoked == ["temporary-access"]
    assert not await conn.fetchval(
        "select onboarding_required from profiles where id=$1", world.teacher_id
    )
    assert await conn.fetchval(
        "select consumed_at from account_activations where id=$1", UUID(body["activation_id"])
    )


@pytest.mark.parametrize(
    "invalid",
    ["expired", "consumed", "superseded", "issuer", "recipient", "email", "wrong_hash", "unbound"],
)
async def test_invalid_activation_never_consumes_provider_proof_or_changes_password(
    conn: asyncpg.Connection, world: World, invalid: str
) -> None:
    clock = FakeClock()
    _, body, identity = await issued(conn, world, clock)
    if invalid == "expired":
        clock.advance(hours=2)
    elif invalid in ("consumed", "superseded"):
        column = "consumed_at" if invalid == "consumed" else "superseded_at"
        await conn.execute(
            f"update account_activations set {column}=now() where user_id=$1", world.teacher_id
        )
    elif invalid in ("issuer", "recipient"):
        await conn.execute(
            "update school_memberships set status='inactive' where user_id=$1",
            world.admin_id if invalid == "issuer" else world.teacher_id,
        )
    elif invalid == "email":
        await conn.execute(
            "update profiles set contact_email='changed@test.nalar' where id=$1", world.teacher_id
        )
    elif invalid == "unbound":
        await conn.execute(
            "update account_activations set code_hash=null where user_id=$1", world.teacher_id
        )
    else:
        body["token_hash"] = "wrong-proof"
    async with api_client(conn, clock=clock, identity=identity) as api:
        response = await api.post("/auth/activate", json=body)
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "INVALID_ACTIVATION"
    assert identity.updated == [] and identity.verifications == 0
    assert body["password"] not in response.text and body["token_hash"] not in response.text


@pytest.mark.parametrize("mismatch", ["subject", "email"])
async def test_verified_proof_must_match_recipient_identity(
    conn: asyncpg.Connection, world: World, mismatch: str
) -> None:
    clock = FakeClock()
    _, body, identity = await issued(conn, world, clock)
    if mismatch == "subject":
        identity.user_id = world.student_id
    else:
        identity.email = "other@test.nalar"
    async with api_client(conn, clock=clock, identity=identity) as api:
        response = await api.post("/auth/activate", json=body)
    assert response.status_code == 400 and response.json()["error"]["code"] == "INVALID_ACTIVATION"
    assert identity.updated == [] and identity.revoked == ["temporary-access"]


async def test_weak_password_and_csrf_are_rejected_before_verification(
    conn: asyncpg.Connection, world: World
) -> None:
    clock = FakeClock()
    _, body, identity = await issued(conn, world, clock)
    async with api_client(conn, clock=clock, identity=identity) as api:
        weak = await api.post("/auth/activate", json={**body, "password": "short"})
        cross_origin = await api.post(
            "/auth/activate", json=body, headers={"Origin": "https://attacker.test"}
        )
        api.headers.pop("X-Nalar-CSRF")
        missing_csrf = await api.post("/auth/activate", json=body)
    assert weak.status_code == 400 and weak.json()["error"]["code"] == "WEAK_PASSWORD"
    assert cross_origin.status_code == missing_csrf.status_code == 403
    assert identity.verifications == 0


async def test_activation_cannot_be_superseded_and_rechecks_scope_after_verification(
    conn: asyncpg.Connection, world: World
) -> None:
    clock = FakeClock()
    notification_id, body, identity = await issued(conn, world, clock)
    clock.advance(seconds=61)

    async def revoke_after_resend_attempt() -> None:
        async with api_client(conn, clock=clock) as api:
            resend = await api.post(
                f"/schools/{world.school_id}/account-invitations",
                json={"user_ids": [str(world.teacher_id)], "resend": True},
                headers=as_user(world.admin_id),
            )
        assert resend.json()["items"][0]["reason"] == "in_progress"
        await conn.execute(
            "update school_memberships set status='inactive' where user_id=$1", world.teacher_id
        )

    identity.before_verify = revoke_after_resend_attempt
    async with api_client(conn, clock=clock, identity=identity) as api:
        response = await api.post("/auth/activate", json=body)
    assert response.status_code == 400 and identity.updated == []
    assert (await state(conn, notification_id))[0] == "failed"


async def test_password_provider_failure_preserves_onboarding_for_explicit_resend(
    conn: asyncpg.Connection, world: World
) -> None:
    clock = FakeClock()
    notification_id, body, identity = await issued(conn, world, clock)
    identity.update_error = True
    async with api_client(conn, clock=clock, identity=identity) as api:
        failed = await api.post("/auth/activate", json=body)
    assert failed.status_code == 503 and "Set-Cookie" not in failed.headers
    assert identity.updated == [] and identity.revoked == ["temporary-access"]
    assert (await state(conn, notification_id))[0] == "failed"
    assert await conn.fetchval(
        "select onboarding_required from profiles where id=$1", world.teacher_id
    )


async def test_password_success_with_completion_failure_is_reconciled_by_normal_login(
    conn: asyncpg.Connection, world: World
) -> None:
    clock = FakeClock()
    _, body, identity = await issued(conn, world, clock)
    await conn.execute("""create function pg_temp.reject_activation_completion() returns trigger
        language plpgsql as $$ begin raise exception 'completion unavailable'; end $$;
        create trigger activation_completion_failure before update of consumed_at
        on account_activations
        for each row execute function pg_temp.reject_activation_completion();""")
    async with api_client(conn, clock=clock, identity=identity) as api:
        failed = await api.post("/auth/activate", json=body)
        assert failed.status_code == 503 and identity.updated == [PASSWORD]
        assert await conn.fetchval(
            "select onboarding_required from profiles where id=$1", world.teacher_id
        )
        blocked_login = await api.post(
            "/auth/login", json={"email": identity.email, "password": PASSWORD}
        )
        assert blocked_login.status_code == 503 and "Set-Cookie" not in blocked_login.headers
        await conn.execute("drop trigger activation_completion_failure on account_activations")
        login = await api.post("/auth/login", json={"email": identity.email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    assert not await conn.fetchval(
        "select onboarding_required from profiles where id=$1", world.teacher_id
    )
    assert await conn.fetchval(
        "select consumed_at from account_activations where id=$1", UUID(body["activation_id"])
    )


async def test_activation_attempts_are_rate_limited_without_blocking_password_login(
    conn: asyncpg.Connection,
) -> None:
    body = {"activation_id": str(uuid4()), "token_hash": "invalid-proof", "password": PASSWORD}
    async with api_client(conn) as api:
        for _ in range(10):
            assert (await api.post("/auth/activate", json=body)).status_code == 400
        assert (await api.post("/auth/activate", json=body)).status_code == 429
        assert (
            await api.post(
                "/auth/login",
                json={"email": "user@test.nalar", "password": FakeIdentityProvider.password},
            )
        ).status_code == 200


async def test_old_proof_cannot_be_used_with_a_new_generation_id(
    conn: asyncpg.Connection, world: World
) -> None:
    clock = FakeClock()
    _, old_body, identity = await issued(conn, world, clock)
    clock.advance(hours=2)
    async with api_client(conn, clock=clock, identity=identity) as api:
        resent = await api.post(
            f"/schools/{world.school_id}/account-invitations",
            json={"user_ids": [str(world.teacher_id)], "resend": True},
            headers=as_user(world.admin_id),
        )
        notification_id = UUID(resent.json()["notification_ids"][0])
        await SendAccountInvitationHandler(uow_on(conn), clock, FakeAuth(conn), TIMING).execute(
            notification_id
        )
        _, new_payload = await state(conn, notification_id)
        result = await api.post(
            "/auth/activate", json={**old_body, "activation_id": new_payload["activation_id"]}
        )
    assert result.status_code == 400 and identity.verifications == 0 and identity.updated == []


async def test_provider_verify_outage_can_retry_the_unconsumed_bound_proof(
    conn: asyncpg.Connection, world: World
) -> None:
    class Outage(RecoveryIdentity):
        async def verify_recovery(self, token_hash: str) -> RecoveryProof:
            if self.verifications == 0:
                self.verifications += 1
                raise DependencyUnavailable()
            return await super().verify_recovery(token_hash)

    clock = FakeClock()
    _, body, identity = await issued(conn, world, clock)
    auth = Outage(identity.user_id, identity.email, identity.token_hash)
    async with api_client(conn, clock=clock, identity=auth) as api:
        assert (await api.post("/auth/activate", json=body)).status_code == 503
        assert (await api.post("/auth/activate", json=body)).status_code == 204
    assert auth.updated == [PASSWORD]
