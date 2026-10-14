import csv
from dataclasses import dataclass
from io import StringIO
from uuid import UUID

from nalar.infrastructure.db.pool import DbConnection

COLUMNS = (
    "misconception_id",
    "library_id",
    "similarity",
    "statement",
    "library_statement",
    "approved",
)


@dataclass(frozen=True)
class Proposal:
    misconception_id: UUID
    library_id: UUID
    similarity: float
    statement: str
    library_statement: str


async def propose(
    conn: DbConnection, kb_id: UUID, subject: str, phase: str, min_similarity: float
) -> list[Proposal]:
    """Best published library match per unlinked, embedded misconception; writes nothing."""
    rows = await conn.fetch(
        "select m.id, m.statement, lib.library_id, lib.statement as library_statement,"
        " lib.similarity from misconceptions m"
        " cross join lateral match_misconception_library($2, $3, m.embedding, m.embedding_model, 1)"
        " lib where m.knowledge_base_id = $1 and m.archived_at is null and m.library_id is null"
        " and m.embedding is not null and lib.similarity >= $4"
        " order by lib.similarity desc, m.id",
        kb_id,
        subject,
        phase,
        min_similarity,
    )
    return [
        Proposal(r["id"], r["library_id"], r["similarity"], r["statement"], r["library_statement"])
        for r in rows
    ]


def review_sheet(proposals: list[Proposal]) -> str:
    out = StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow(COLUMNS)
    for p in proposals:
        writer.writerow(
            [
                p.misconception_id,
                p.library_id,
                f"{p.similarity:.4f}",
                p.statement,
                p.library_statement,
                "",
            ]
        )
    return out.getvalue()


def approved_pairs(sheet: str) -> list[tuple[UUID, UUID]]:
    return [
        (UUID(row["misconception_id"]), UUID(row["library_id"]))
        for row in csv.DictReader(StringIO(sheet))
        if row["approved"].strip().lower() == "yes"
    ]


async def apply(conn: DbConnection, kb_id: UUID, pairs: list[tuple[UUID, UUID]]) -> int:
    """Links only still-unlinked misconceptions of this KB to published entries; audited."""
    linked = 0
    for misconception_id, library_id in pairs:
        school_id = await conn.fetchval(
            "update misconceptions m set library_id = $3 where m.id = $2"
            " and m.knowledge_base_id = $1 and m.archived_at is null and m.library_id is null"
            " and exists (select 1 from misconception_library l where l.id = $3"
            " and l.status = 'published') returning m.school_id",
            kb_id,
            misconception_id,
            library_id,
        )
        if school_id is None:
            continue
        await conn.execute(
            "insert into audit_logs (school_id, action, entity_table, entity_id, changes)"
            " values ($1, 'misconception.library_linked', 'misconceptions', $2,"
            " jsonb_build_object('library_id', $3::text))",
            school_id,
            misconception_id,
            str(library_id),
        )
        linked += 1
    return linked
