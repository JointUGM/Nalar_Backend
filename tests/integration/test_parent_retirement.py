from pathlib import Path

import asyncpg
import pytest

from nalar.application.errors import Conflict
from nalar.infrastructure.db.repositories.activations import PgActivationsRepo
from nalar.infrastructure.db.repositories.password_resets import PgPasswordResetsRepo
from nalar.infrastructure.db.repositories.publications import PgPublicationsRepo
from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import World
from tests.unit.application.fakes import FakeClock

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "supabase/migrations/20261014000000_remove_parent_jobs.sql"
).read_text(encoding="utf-8")


async def test_legacy_parent_links_do_not_grant_accounts_or_notifications(
    conn: asyncpg.Connection, world: World
) -> None:
    activations = PgActivationsRepo(conn)
    resets = PgPasswordResetsRepo(conn)
    assert await activations.request_recipients(world.school_id, [world.parent_id]) == []
    assert await resets.recipient(world.parent_id, recovery=False) is None
    async with api_client(conn) as api:
        assert (
            await api.get("/notifications", headers=as_user(world.parent_id))
        ).status_code == 404
        response = await api.get(
            f"/schools/{world.school_id}/people", headers=as_user(world.admin_id)
        )
        assert response.status_code == 200
        assert str(world.parent_id) not in {item["user_id"] for item in response.json()["items"]}


async def test_retirement_migration_disables_parent_rls_and_cron(
    conn: asyncpg.Connection, world: World
) -> None:
    await conn.execute(MIGRATION)
    assert not await conn.fetchval("select public.parent_can_see_session($1)", world.session_id)
    assert not await conn.fetchval(
        "select public.parent_can_see_publication($1, $2)", world.publication_id, world.student_id
    )
    assert (
        await conn.fetchval(
            "select count(*) from cron.job"
            " where jobname in ('nalar-weekly-digest', 'nalar-release-reminders')"
        )
        == 0
    )


async def test_cancelled_publication_still_rejects_window_edits(
    conn: asyncpg.Connection, world: World
) -> None:
    await conn.execute(
        "update publications set cancelled_at = now() where id = $1", world.publication_id
    )
    with pytest.raises(Conflict, match="PUBLICATION_NOT_EDITABLE"):
        await PgPublicationsRepo(conn).edit(
            world.publication_id, FakeClock().now(), None, None, cancel=False
        )
