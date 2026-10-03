import json
from collections import defaultdict
from datetime import datetime
from uuid import UUID

from nalar.application.ports.results import (
    AttentionCounts,
    AttentionCursor,
    AttentionItem,
    ClassMapInput,
    FlagAttention,
    KbReviewAttention,
    Monitor,
    MonitorRun,
    MonitorStudent,
    ReleaseReadyAttention,
    ReportConceptResult,
    ReportFlag,
    ReportOverride,
    ReportScore,
    ReportTurn,
    SafetyAttention,
    SessionReport,
)
from nalar.domain.class_map import ConceptResult, StudentAttempt
from nalar.infrastructure.db.pool import DbConnection
from nalar.infrastructure.db.repositories.release import ELIGIBLE_SQL

_ATTENTION = f"""
    with scoped_publications as materialized (
        select p.id, p.class_id, p.created_at, p.released_to_parents_at, p.cancelled_at,
               c.name as class_name, mi.title as mission_title
          from publications p
          join classes c on c.id = p.class_id and c.archived_at is null
          join mission_versions mv on mv.id = p.mission_version_id
          join missions mi on mi.id = mv.mission_id
          join knowledge_bases kb on kb.id = mi.knowledge_base_id
         where p.school_id = $2 and exists (
             select 1 from teaching_assignments ta
              where ta.school_id = $2 and ta.class_id = p.class_id
                and ta.school_subject_id = kb.school_subject_id and ta.teacher_id = $1)
    ), scoped_sessions as materialized (
        select s.*, pr.full_name as student_name
          from sessions s
          join scoped_publications p on p.id = s.publication_id
          join profiles pr on pr.id = s.student_id
    ), scoped_kbs as (
        select id, topic_title from knowledge_bases
         where school_id = $2 and owner_teacher_id = $1 and status <> 'generating'
    ), pending_items as (
        select c.knowledge_base_id, 'concept' as kind, c.created_at
          from concepts c join scoped_kbs kb on kb.id = c.knowledge_base_id
         where c.review_status = 'pending' and c.archived_at is null
        union all
        select m.knowledge_base_id, 'misconception', m.created_at
          from misconceptions m join scoped_kbs kb on kb.id = m.knowledge_base_id
         where m.review_status = 'pending' and m.archived_at is null
    ), pending_counts as (
        select knowledge_base_id, max(created_at) as created_at,
               count(*) filter (where kind = 'concept')::int as pending_concepts,
               count(*) filter (where kind = 'misconception')::int as pending_misconceptions
          from pending_items group by knowledge_base_id
    ), ready_publications as (
        select p.*, eligible.eligible_count,
               greatest(p.created_at, eligible.summary_at,
                   (select max(r.closed_at) from publication_runs r where r.publication_id = p.id),
                   (select max(s.ended_at) from scoped_sessions s where s.publication_id = p.id),
                   (select max(e.evaluated_at) from session_evaluations e
                     join scoped_sessions s on s.id = e.session_id where s.publication_id = p.id)
               ) as ready_at
          from scoped_publications p
          cross join lateral (
              select count(*)::int as eligible_count,
                     count(*) filter (where ps.id is null) as missing_summaries,
                     max(ps.generated_at) as summary_at
                from ({ELIGIBLE_SQL.replace("$1", "p.id")}) el
                left join parent_summaries ps
                  on ps.publication_id = p.id and ps.student_id = el.student_id
          ) eligible
         where p.released_to_parents_at is null and p.cancelled_at is null
           and eligible.eligible_count > 0 and eligible.missing_summaries = 0
           and not exists (select 1 from publication_runs r
                            where r.publication_id = p.id and r.status <> 'closed')
           and not exists (select 1 from run_participants rp
                            join publication_runs r on r.id = rp.run_id
                           where r.publication_id = p.id and rp.status = 'waiting')
           and not exists (select 1 from scoped_sessions s
                            where s.publication_id = p.id
                              and s.status in ('in_progress', 'paused_safety'))
           and not exists (select 1 from scoped_sessions s
                            where s.publication_id = p.id
                              and s.status in ('completed', 'timed_out', 'ended_safety')
                              and not exists (select 1 from session_evaluations e
                                               where e.session_id = s.id))
    ), queue as materialized (
        select 'safety' as kind, s.id as item_id,
               coalesce(s.safety_paused_at, s.last_activity_at) as created_at,
               jsonb_build_object('session_id', s.id, 'publication_id', s.publication_id,
                   'student_name', s.student_name, 'paused_at', s.safety_paused_at) as payload
          from scoped_sessions s where s.status = 'paused_safety'
        union all
        select 'flag', f.id, f.created_at,
               jsonb_build_object('flag_id', f.id, 'flag_type', f.flag_type, 'severity', f.severity,
                   'session_id', s.id, 'publication_id', s.publication_id,
                   'student_name', s.student_name)
          from authenticity_flags f join scoped_sessions s on s.id = f.session_id
         where f.status = 'open'
        union all
        select 'kb_review', kb.id, pc.created_at,
               jsonb_build_object('knowledge_base_id', kb.id, 'topic_title', kb.topic_title,
                   'pending_concepts', pc.pending_concepts,
                   'pending_misconceptions', pc.pending_misconceptions)
          from scoped_kbs kb join pending_counts pc on pc.knowledge_base_id = kb.id
        union all
        select 'release_ready', p.id, p.ready_at,
               jsonb_build_object('publication_id', p.id, 'class_name', p.class_name,
                   'mission_title', p.mission_title, 'eligible_count', p.eligible_count)
          from ready_publications p
    ), counts as (
        select count(*) filter (where kind = 'safety')::int as safety,
               count(*) filter (where kind = 'flag')::int as flag,
               count(*) filter (where kind = 'kb_review')::int as kb_review,
               count(*) filter (where kind = 'release_ready')::int as release_ready,
               count(*)::int as total from queue
    ), page as (
        select * from queue
         where $4::timestamptz is null or created_at < $4
            or (created_at = $4 and (kind collate "C", item_id) > ($5::text collate "C", $6::uuid))
         order by created_at desc, kind collate "C", item_id limit $3
    )
    select counts.*, page.* from counts left join page on true
     order by page.created_at desc, page.kind collate "C", page.item_id
"""

_MONITOR_RUN = """
    select r.id, r.mode::text as mode, r.status::text as status, r.join_code, r.started_at,
           mv.max_turns,
           (select count(*) from run_participants rp
             where rp.run_id = r.id and rp.status = 'waiting') as waiting_count
      from publications p
      join publication_runs r on r.publication_id = p.id and r.kind = 'primary'
      join mission_versions mv on mv.id = p.mission_version_id
     where p.id = $1
"""

_MONITOR_STUDENTS = """
    select ce.student_id, pr.full_name as name, s.id as session_id,
           s.status::text as session_status,
           s.current_turn_index, s.deadline_at, rp.status::text as participant_status,
           (select count(*) from authenticity_flags f
             where f.session_id = s.id and f.status = 'open') as open_flag_count
      from publications p
      join class_enrollments ce on ce.class_id = p.class_id and ce.status = 'active'
      join profiles pr on pr.id = ce.student_id
      left join lateral (select * from sessions x
                          where x.run_id = $2 and x.student_id = ce.student_id
                          order by x.attempt_number desc limit 1) s on true
      left join run_participants rp on rp.student_id = ce.student_id and rp.run_id = $2
     where p.id = $1
     order by pr.full_name, ce.student_id
"""

_REPORT_SESSION = """
    select s.student_id, pr.full_name as student_name, s.status::text as status,
           s.end_reason::text as end_reason, s.started_at, s.ended_at, s.attempt_number,
           e.id as evaluation_id, e.status::text as evaluation_status,
           e.summary as evaluation_summary
      from sessions s
      join profiles pr on pr.id = s.student_id
      left join session_evaluations e on e.session_id = s.id
     where s.id = $1
"""

_REPORT_TURNS = """
    select id as turn_id, turn_index, prompt_kind::text as kind, prompt_text as prompt,
           answer_text as answer, prompt_strategy::text as move, move_source::text as move_source,
           move_reason_code::text as reason_code, move_reason as reason,
           guard_result::text as guard_result, answer_state::text as answer_state, safety_paused
      from session_turns where session_id = $1 order by turn_index
"""

_REPORT_SCORES = """
    select id as score_id, dimension::text as dimension, ai_level, final_level, rationale
      from evaluation_scores where evaluation_id = $1 order by dimension
"""

_REPORT_EVIDENCE = """
    select e.score_id, e.turn_id, e.quote
      from score_evidence e join evaluation_scores s on s.id = e.score_id
     where s.evaluation_id = $1
"""

_REPORT_OVERRIDES = """
    select o.score_id, o.previous_level, o.new_level, o.reason, o.created_at
      from score_overrides o join evaluation_scores s on s.id = o.score_id
     where s.evaluation_id = $1 order by o.created_at
"""

_TARGETS = """
    select c.id, c.name
      from publications p
      join mission_version_concepts mvc on mvc.mission_version_id = p.mission_version_id
      join concepts c on c.id = mvc.concept_id
     where p.id = $1 order by c.name, c.id
"""

_VERSION_MISCONCEPTIONS = """
    select m.id, m.concept_id, m.statement
      from publications p
      join mission_version_misconceptions mvm on mvm.mission_version_id = p.mission_version_id
      join misconceptions m on m.id = mvm.misconception_id
     where p.id = $1 order by m.statement, m.id
"""

# TC-9: only each student's latest attempt counts; an older attempt is never reused.
_LATEST_ATTEMPTS = """
    select distinct on (s.student_id) s.id, s.student_id, s.status::text as status,
           e.status::text as evaluation_status
      from sessions s left join session_evaluations e on e.session_id = s.id
     where s.publication_id = $1
     order by s.student_id, s.attempt_number desc
"""

_CONCEPT_RESULTS = """
    select session_id, concept_id, outcome::text as outcome, misconception_id,
           initial_misconception_id, resolved_in_session
      from session_concept_results where session_id = any($1::uuid[])
"""


class PgResultsRepo:
    async def teacher_attention(
        self, actor_id: UUID, school_id: UUID, limit: int, after: AttentionCursor | None
    ) -> tuple[list[AttentionItem], AttentionCounts]:
        rows = await self._conn.fetch(
            _ATTENTION, actor_id, school_id, limit, *(after or (None, None, None))
        )
        counts = AttentionCounts(
            *(rows[0][key] for key in ("safety", "flag", "kb_review", "release_ready", "total"))
        )
        items: list[AttentionItem] = []
        for row in rows:
            if row["kind"] is None:
                continue
            data = json.loads(row["payload"])
            item_id, created_at = row["item_id"], row["created_at"]
            if row["kind"] == "safety":
                items.append(
                    SafetyAttention(
                        item_id,
                        created_at,
                        UUID(data["session_id"]),
                        UUID(data["publication_id"]),
                        data["student_name"],
                        datetime.fromisoformat(data["paused_at"]) if data["paused_at"] else None,
                    )
                )
            elif row["kind"] == "flag":
                items.append(
                    FlagAttention(
                        item_id,
                        created_at,
                        UUID(data["flag_id"]),
                        data["flag_type"],
                        data["severity"],
                        UUID(data["session_id"]),
                        UUID(data["publication_id"]),
                        data["student_name"],
                    )
                )
            elif row["kind"] == "kb_review":
                items.append(
                    KbReviewAttention(
                        item_id,
                        created_at,
                        UUID(data["knowledge_base_id"]),
                        data["topic_title"],
                        data["pending_concepts"],
                        data["pending_misconceptions"],
                    )
                )
            else:
                items.append(
                    ReleaseReadyAttention(
                        item_id,
                        created_at,
                        UUID(data["publication_id"]),
                        data["class_name"],
                        data["mission_title"],
                        data["eligible_count"],
                    )
                )
        return items, counts

    def __init__(self, conn: DbConnection) -> None:
        self._conn = conn

    async def monitor(self, publication_id: UUID) -> Monitor | None:
        run = await self._conn.fetchrow(_MONITOR_RUN, publication_id)
        if run is None:
            return None
        rows = await self._conn.fetch(_MONITOR_STUDENTS, publication_id, run["id"])
        students = tuple(
            MonitorStudent(
                student_id=r["student_id"],
                session_id=r["session_id"],
                name=r["name"],
                status=r["session_status"] or r["participant_status"] or "not_joined",
                current_turn_index=r["current_turn_index"],
                deadline_at=r["deadline_at"],
                open_flag_count=r["open_flag_count"],
                safety_paused=r["session_status"] == "paused_safety",
            )
            for r in rows
        )
        return Monitor(
            run=MonitorRun(
                run["id"], run["mode"], run["status"], run["join_code"], run["started_at"]
            ),
            max_turns=run["max_turns"],
            waiting_count=run["waiting_count"],
            students=students,
        )

    async def report(self, session_id: UUID) -> SessionReport | None:
        head = await self._conn.fetchrow(_REPORT_SESSION, session_id)
        if head is None:
            return None
        turns = await self._conn.fetch(_REPORT_TURNS, session_id)
        scores: tuple[ReportScore, ...] = ()
        evaluation_id = head["evaluation_id"]
        if evaluation_id is not None:
            evidence: defaultdict[UUID, list[tuple[UUID, str]]] = defaultdict(list)
            for e in await self._conn.fetch(_REPORT_EVIDENCE, evaluation_id):
                evidence[e["score_id"]].append((e["turn_id"], e["quote"]))
            overrides: defaultdict[UUID, list[ReportOverride]] = defaultdict(list)
            for o in await self._conn.fetch(_REPORT_OVERRIDES, evaluation_id):
                overrides[o["score_id"]].append(
                    ReportOverride(
                        o["previous_level"], o["new_level"], o["reason"], o["created_at"]
                    )
                )
            scores = tuple(
                ReportScore(
                    **dict(s),
                    evidence=tuple(evidence[s["score_id"]]),
                    overrides=tuple(overrides[s["score_id"]]),
                )
                for s in await self._conn.fetch(_REPORT_SCORES, evaluation_id)
            )
        results = await self._conn.fetch(
            "select concept_id, outcome::text as outcome, misconception_id, resolved_in_session"
            " from session_concept_results where session_id = $1 order by concept_id",
            session_id,
        )
        flags = await self._conn.fetch(
            "select id, flag_type::text as flag_type, severity::text as severity,"
            " status::text as status from authenticity_flags where session_id = $1"
            " order by created_at",
            session_id,
        )
        data = dict(head)
        data.pop("evaluation_id")
        return SessionReport(
            **data,
            turns=tuple(ReportTurn(**dict(t)) for t in turns),
            scores=scores,
            concept_results=tuple(ReportConceptResult(**dict(r)) for r in results),
            flags=tuple(ReportFlag(**dict(f)) for f in flags),
        )

    async def class_map_input(self, publication_id: UUID) -> ClassMapInput | None:
        exists = await self._conn.fetchval(
            "select exists (select 1 from publications where id = $1)", publication_id
        )
        if not exists:
            return None
        concepts = await self._conn.fetch(_TARGETS, publication_id)
        misconceptions = await self._conn.fetch(_VERSION_MISCONCEPTIONS, publication_id)
        latest = await self._conn.fetch(_LATEST_ATTEMPTS, publication_id)
        by_session: defaultdict[UUID, list[ConceptResult]] = defaultdict(list)
        for r in await self._conn.fetch(_CONCEPT_RESULTS, [a["id"] for a in latest]):
            by_session[r["session_id"]].append(
                ConceptResult(
                    r["concept_id"],
                    r["outcome"],
                    r["misconception_id"],
                    r["initial_misconception_id"],
                    r["resolved_in_session"],
                )
            )
        return ClassMapInput(
            concepts=tuple((c["id"], c["name"]) for c in concepts),
            misconceptions=tuple(
                (m["id"], m["concept_id"], m["statement"]) for m in misconceptions
            ),
            attempts=tuple(
                StudentAttempt(
                    a["student_id"], a["status"], a["evaluation_status"], tuple(by_session[a["id"]])
                )
                for a in latest
            ),
        )
