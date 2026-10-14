from uuid import UUID

import asyncpg

from nalar.bootstrap.library_relink import apply, approved_pairs, propose, review_sheet
from nalar.bootstrap.settings import Settings
from tests.integration.support.factories import World, build_world
from tests.unit.application.fakes import vector

MODEL = Settings().embedding_model


async def library_entry(conn: asyncpg.Connection, status: str, at: int) -> UUID:
    library_id: UUID = await conn.fetchval(
        "insert into misconception_library (subject, phase, topic, statement,"
        " correct_understanding, source_citations, status, published_at, embedding,"
        " embedding_model) values ('Ilmu Pengetahuan Alam', 'D', 'Gaya', $1, 'x', array['Buku'],"
        " $2::library_status, case when $2 = 'published' then now() end, $3, $4) returning id",
        f"entri {status} {at}",
        status,
        vector(at),
        MODEL,
    )
    return library_id


async def test_dry_run_writes_nothing_and_apply_links_only_approved_published_rows(
    conn: asyncpg.Connection, world: World
) -> None:
    published = await library_entry(conn, "published", 0)
    draft = await library_entry(conn, "draft", 1)
    first, second = world.misconception_ids
    for misconception_id, at in ((first, 0), (second, 1)):
        await conn.execute(
            "update misconceptions set embedding = $2, embedding_model = $3 where id = $1",
            misconception_id,
            vector(at),
            MODEL,
        )
    proposals = await propose(conn, world.kb_id, "Ilmu Pengetahuan Alam", "D", 0.5)
    assert [(p.misconception_id, p.library_id) for p in proposals] == [(first, published)]
    assert (
        await conn.fetchval("select count(*) from misconceptions where library_id is not null") == 0
    )
    sheet = review_sheet(proposals).replace(",\r\n", ",yes\r\n", 1)
    pairs = approved_pairs(sheet) + [(second, draft), (second, published)]
    assert await apply(conn, world.kb_id, pairs) == 2
    assert await apply(conn, world.kb_id, pairs) == 0
    assert (
        await conn.fetchval("select library_id from misconceptions where id = $1", first)
        == published
    )
    assert (
        await conn.fetchval(
            "select count(*) from audit_logs where action = 'misconception.library_linked'"
        )
        == 2
    )


async def test_apply_rejects_a_misconception_of_another_knowledge_base(
    conn: asyncpg.Connection, world: World
) -> None:
    published = await library_entry(conn, "published", 0)
    other = await build_world(conn, "SMP Lain")
    assert await apply(conn, world.kb_id, [(other.misconception_ids[0], published)]) == 0
