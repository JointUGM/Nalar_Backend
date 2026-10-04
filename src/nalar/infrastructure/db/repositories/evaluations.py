import json
from uuid import UUID

from nalar.application.ports.evaluations import (
    ConceptResultRow,
    EvalTurn,
    EvaluationInput,
    ReflectionView,
)
from nalar.domain.labels import EvaluationStatus, SessionStatus
from nalar.infrastructure.db.pool import DbConnection

_INPUT = """
    select s.id, s.school_id, s.status::text as status,
           exists (select 1 from session_evaluations e where e.session_id = s.id) as evaluated,
           mv.context_pack, mv.rubric
      from sessions s
      join publications p on p.id = s.publication_id
      join mission_versions mv on mv.id = p.mission_version_id
     where s.id = $1
"""

_TURNS = """
    select id, turn_index, prompt_kind::text as kind, prompt_text as question_text, answer_text,
           prompt_strategy::text as move, target_concept_id
      from session_turns where session_id = $1 order by turn_index
"""

_REFLECTION = """
    select s.status::text as status,
           exists (select 1 from session_evaluations e where e.session_id = s.id) as evaluated,
           mi.title as mission_title, s.ended_at, r.content, rp.warmup_choice_id,
           mv.live_warmup, ss.name as subject_name
      from sessions s
      join publications p on p.id = s.publication_id
      join mission_versions mv on mv.id = p.mission_version_id
      join missions mi on mi.id = mv.mission_id
      join knowledge_bases kb on kb.id = mi.knowledge_base_id
      join school_subjects ss on ss.id = kb.school_subject_id
      left join session_reflections r on r.session_id = s.id
      left join run_participants rp on rp.session_id = s.id
     where s.id = $1
"""


class PgEvaluationsRepo:
    def __init__(self, conn: DbConnection) -> None:
        self._conn = conn

    async def evaluation_input(self, session_id: UUID) -> EvaluationInput | None:
        row = await self._conn.fetchrow(_INPUT, session_id)
        if row is None:
            return None
        turns = await self._conn.fetch(_TURNS, session_id)
        return EvaluationInput(
            session_id=row["id"],
            school_id=row["school_id"],
            status=SessionStatus(row["status"]),
            evaluated=row["evaluated"],
            context_pack=json.loads(row["context_pack"]),
            rubric=json.loads(row["rubric"]),
            turns=tuple(EvalTurn(**dict(t)) for t in turns),
        )

    async def insert(
        self,
        session_id: UUID,
        school_id: UUID,
        status: EvaluationStatus,
        summary: str | None,
        turn_quality_json: str,
        ai_invocation_id: UUID | None,
    ) -> UUID | None:
        evaluation_id: UUID | None = await self._conn.fetchval(
            "insert into session_evaluations"
            " (school_id, session_id, status, summary, turn_quality, ai_invocation_id)"
            " values ($1, $2, $3::evaluation_status, $4, $5::jsonb, $6)"
            " on conflict (session_id) do nothing returning id",
            school_id,
            session_id,
            status.value,
            summary,
            turn_quality_json,
            ai_invocation_id,
        )
        return evaluation_id

    async def insert_score(
        self, school_id: UUID, evaluation_id: UUID, dimension: str, level: int, rationale: str
    ) -> UUID:
        score_id: UUID = await self._conn.fetchval(
            "insert into evaluation_scores"
            " (school_id, evaluation_id, dimension, ai_level, final_level, rationale)"
            " values ($1, $2, $3::rubric_dimension, $4, $4, $5) returning id",
            school_id,
            evaluation_id,
            dimension,
            level,
            rationale,
        )
        return score_id

    async def insert_evidence(
        self, school_id: UUID, score_id: UUID, turn_id: UUID, quote: str
    ) -> None:
        await self._conn.execute(
            "insert into score_evidence (school_id, score_id, turn_id, quote)"
            " values ($1, $2, $3, $4)",
            school_id,
            score_id,
            turn_id,
            quote,
        )

    async def insert_concept_result(
        self, school_id: UUID, session_id: UUID, result: ConceptResultRow
    ) -> None:
        await self._conn.execute(
            "insert into session_concept_results (school_id, session_id, concept_id, outcome,"
            " misconception_id, initial_misconception_id, resolved_in_session, evidence_turn_id)"
            " values ($1, $2, $3, $4::concept_outcome, $5, $6, $7, $8)",
            school_id,
            session_id,
            result.concept_id,
            result.outcome,
            result.misconception_id,
            result.initial_misconception_id,
            result.resolved_in_session,
            result.evidence_turn_id,
        )

    async def insert_reflection(
        self, school_id: UUID, session_id: UUID, content: str, ai_invocation_id: UUID | None
    ) -> None:
        await self._conn.execute(
            "insert into session_reflections (school_id, session_id, content, ai_invocation_id)"
            " values ($1, $2, $3, $4) on conflict (session_id) do nothing",
            school_id,
            session_id,
            content,
            ai_invocation_id,
        )

    async def reflection_view(self, session_id: UUID) -> ReflectionView | None:
        row = await self._conn.fetchrow(_REFLECTION, session_id)
        if row is None:
            return None
        data = dict(row)
        warmup = data.pop("live_warmup")
        return ReflectionView(
            **{**data, "status": SessionStatus(data["status"])},
            live_warmup=json.loads(warmup) if warmup else None,
        )
