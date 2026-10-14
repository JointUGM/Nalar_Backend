import json
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from nalar.application.ports.release import (
    PublicationText,
    StoredInsight,
)
from nalar.infrastructure.db.pool import DbConnection

_TEXT = """
    select p.id, p.school_id, mi.title as mission_title
      from publications p
      join mission_versions mv on mv.id = p.mission_version_id
      join missions mi on mi.id = mv.mission_id
     where p.id = $1
"""


class PgReleaseRepo:
    def __init__(self, conn: DbConnection) -> None:
        self._conn = conn

    async def publication_text(self, publication_id: UUID) -> PublicationText | None:
        row = await self._conn.fetchrow(_TEXT, publication_id)
        return PublicationText(**dict(row)) if row else None

    async def lock(self, publication_id: UUID) -> PublicationText | None:
        await self._conn.execute(
            "select 1 from publications where id = $1 for update", publication_id
        )
        return await self.publication_text(publication_id)

    async def has_insight(self, publication_id: UUID) -> bool:
        return bool(
            await self._conn.fetchval(
                "select exists (select 1 from class_map_insights where publication_id = $1)",
                publication_id,
            )
        )

    async def insert_insight(
        self,
        school_id: UUID,
        publication_id: UUID,
        counts_snapshot: Mapping[str, Any],
        clusters: Sequence[Mapping[str, Any]],
        narrative: str,
        ai_invocation_id: UUID | None,
        suggestions: Sequence[str],
    ) -> None:
        pub = await self.lock(publication_id)
        if pub is None or pub.school_id != school_id or await self.has_insight(publication_id):
            return
        insight_id = await self._conn.fetchval(
            "insert into class_map_insights (school_id, publication_id, counts_snapshot,"
            " clusters, narrative, ai_invocation_id) values ($1, $2, $3::jsonb, $4::jsonb, $5, $6)"
            " returning id",
            school_id,
            publication_id,
            json.dumps(dict(counts_snapshot), default=str),
            json.dumps([dict(c) for c in clusters], default=str),
            narrative,
            ai_invocation_id,
        )
        if suggestions:
            await self._conn.executemany(
                "insert into follow_up_suggestions (school_id, insight_id, rank, content)"
                " values ($1, $2, $3, $4)",
                [
                    (school_id, insight_id, rank, text)
                    for rank, text in enumerate(suggestions, start=1)
                ],
            )

    async def latest_insight(self, publication_id: UUID) -> StoredInsight | None:
        row = await self._conn.fetchrow(
            "select i.narrative, i.counts_snapshot, i.generated_at,"
            " array(select content from follow_up_suggestions"
            " where insight_id = i.id order by rank)"
            " as suggestions from class_map_insights i"
            " where publication_id = $1 order by generated_at desc limit 1",
            publication_id,
        )
        if row is None:
            return None
        return StoredInsight(
            row["narrative"],
            json.loads(row["counts_snapshot"]),
            row["generated_at"],
            tuple(row["suggestions"]),
        )

    async def finalize_runs(self, publication_id: UUID) -> int:
        runs: int = await self._conn.fetchval(
            "select count(*) from jobs where kind = 'publication_finalize' and entity_id = $1"
            " and status = 'succeeded'",
            publication_id,
        )
        return runs
