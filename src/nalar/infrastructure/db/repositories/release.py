import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from nalar.application.ports.release import (
    PublicationText,
    StoredInsight,
    SummaryConcept,
    SummaryInput,
    SummaryPreview,
)
from nalar.domain.release import ReleaseCounts
from nalar.infrastructure.db.pool import DbConnection

# TC-18 eligibility, shared with the parent visibility rule: the student's latest finished
# attempt is completed and has a completed evaluation.
ELIGIBLE_SQL = """
    select ls.student_id, ls.id as session_id, e.summary as evaluation_summary
      from v_latest_sessions ls
      join session_evaluations e on e.session_id = ls.id and e.status = 'completed'
     where ls.publication_id = $1 and ls.status = 'completed'
"""

_TEXT = """
    select p.id, p.school_id, mi.title as mission_title, p.released_to_parents_at as released_at
      from publications p
      join mission_versions mv on mv.id = p.mission_version_id
      join missions mi on mi.id = mv.mission_id
     where p.id = $1
"""

_COUNTS = f"""
    with eligible as ({ELIGIBLE_SQL})
    select
      (select count(*) from publication_runs
        where publication_id = $1 and status <> 'closed')::int as open_runs,
      (select count(*) from sessions
        where publication_id = $1 and status in ('in_progress', 'paused_safety'))::int
          as active_sessions,
      (select count(*) from sessions s
        where s.publication_id = $1 and s.status in ('completed', 'timed_out', 'ended_safety')
          and not exists (select 1 from session_evaluations e where e.session_id = s.id))::int
          as unevaluated_sessions,
      (select count(*) from eligible)::int as eligible,
      (select count(*) from eligible el
        where not exists (select 1 from parent_summaries ps
                           where ps.publication_id = $1 and ps.student_id = el.student_id))::int
          as eligible_without_summary,
      (select count(*) from v_latest_sessions ls
        where ls.publication_id = $1
          and ls.student_id not in (select student_id from eligible))::int as ineligible
"""

# INTEGRATION.md, S5: send the misconception statement when the outcome is a misconception or
# when the child resolved one (the idea they moved away from).
_SUMMARY_INPUT = f"""
    with eligible as ({ELIGIBLE_SQL})
    select el.student_id, el.evaluation_summary,
           coalesce(json_agg(json_build_object(
               'name', c.name,
               'outcome', r.outcome::text,
               'resolved', r.resolved_in_session,
               'statement', case when r.outcome = 'misconception' or r.resolved_in_session
                                 then x.statement end) order by c.name)
             filter (where r.id is not null), '[]') as concepts
      from eligible el
      left join session_concept_results r on r.session_id = el.session_id
      left join concepts c on c.id = r.concept_id
      left join misconceptions x on x.id = coalesce(r.misconception_id, r.initial_misconception_id)
     where not exists (select 1 from parent_summaries ps
                        where ps.publication_id = $1 and ps.student_id = el.student_id)
     group by el.student_id, el.evaluation_summary
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

    async def counts(self, publication_id: UUID) -> ReleaseCounts:
        row = await self._conn.fetchrow(_COUNTS, publication_id)
        assert row is not None
        return ReleaseCounts(**dict(row))

    async def preview_summaries(self, publication_id: UUID) -> list[SummaryPreview]:
        rows = await self._conn.fetch(
            f"with eligible as ({ELIGIBLE_SQL})"
            " select el.student_id, pr.full_name as name, ps.content as summary_text"
            "   from eligible el"
            "   join parent_summaries ps"
            "     on ps.publication_id = $1 and ps.student_id = el.student_id"
            "   join profiles pr on pr.id = el.student_id"
            "  order by pr.full_name",
            publication_id,
        )
        return [SummaryPreview(**dict(r)) for r in rows]

    async def release(self, publication_id: UUID, actor_id: UUID, now: datetime) -> None:
        await self._conn.execute(
            "update publications set released_to_parents_at = $2, released_by = $3"
            " where id = $1 and released_to_parents_at is null",
            publication_id,
            now,
            actor_id,
        )

    async def summary_input(self, publication_id: UUID) -> list[SummaryInput]:
        rows = await self._conn.fetch(_SUMMARY_INPUT, publication_id)
        return [
            SummaryInput(
                r["student_id"],
                tuple(
                    SummaryConcept(c["name"], c["outcome"], c["statement"], c["resolved"])
                    for c in json.loads(r["concepts"])
                ),
                r["evaluation_summary"],
            )
            for r in rows
        ]

    async def insert_summary(
        self,
        school_id: UUID,
        publication_id: UUID,
        student_id: UUID,
        text: str,
        ai_invocation_id: UUID | None,
    ) -> bool:
        return (
            await self._conn.fetchval(
                "insert into parent_summaries"
                " (school_id, publication_id, student_id, content, ai_invocation_id)"
                " select $1, $2, $3, $4, $5 from publications p"
                "  where p.id = $2 and p.released_to_parents_at is null"
                " on conflict (publication_id, student_id) do nothing returning id",
                school_id,
                publication_id,
                student_id,
                text,
                ai_invocation_id,
            )
            is not None
        )

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
    ) -> None:
        await self._conn.execute(
            "insert into class_map_insights (school_id, publication_id, counts_snapshot,"
            " clusters, narrative, ai_invocation_id) values ($1, $2, $3::jsonb, $4::jsonb, $5, $6)",
            school_id,
            publication_id,
            json.dumps(dict(counts_snapshot), default=str),
            json.dumps([dict(c) for c in clusters], default=str),
            narrative,
            ai_invocation_id,
        )

    async def latest_insight(self, publication_id: UUID) -> StoredInsight | None:
        row = await self._conn.fetchrow(
            "select narrative, counts_snapshot, generated_at from class_map_insights"
            " where publication_id = $1 order by generated_at desc limit 1",
            publication_id,
        )
        if row is None:
            return None
        return StoredInsight(
            row["narrative"], json.loads(row["counts_snapshot"]), row["generated_at"]
        )

    async def finalize_runs(self, publication_id: UUID) -> int:
        runs: int = await self._conn.fetchval(
            "select count(*) from jobs where kind = 'publication_finalize' and entity_id = $1"
            " and status = 'succeeded'",
            publication_id,
        )
        return runs

    async def reminder_candidates(self, finalized_before: datetime) -> list[tuple[UUID, UUID]]:
        rows = await self._conn.fetch(
            "select p.id, p.school_id from publications p"
            " where p.released_to_parents_at is null and p.cancelled_at is null"
            "   and exists (select 1 from jobs j where j.kind = 'publication_finalize'"
            "     and j.entity_id = p.id and j.status = 'succeeded' and j.updated_at < $1)",
            finalized_before,
        )
        return [(r["id"], r["school_id"]) for r in rows]
