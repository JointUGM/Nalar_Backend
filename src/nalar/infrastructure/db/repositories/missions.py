import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from nalar.application.errors import Conflict, NotFound
from nalar.application.ports.ai_contract import (
    ApprovedConceptIn,
    ApprovedMisconceptionIn,
    SourceParagraphIn,
)
from nalar.application.ports.missions import (
    GenerationCatalog,
    MissionRef,
    MissionSummary,
    VersionDraft,
    VersionHistoryEntry,
    VersionRecord,
    VersionSummary,
    version_status,
)
from nalar.domain.context_pack import PackMisconception, Target
from nalar.infrastructure.db.pool import DbConnection

_MISSION_PAGE = """
    select mi.id, mi.title, mi.knowledge_base_id, mi.created_by, mi.created_at,
           v.id as version_id, v.version_number, v.reviewed_at, v.locked_at,
           pr.full_name as created_by_name
      from missions mi
      left join profiles pr on pr.id = mi.created_by
      join knowledge_bases kb on kb.id = mi.knowledge_base_id
      left join lateral (
            select mv.id, mv.version_number, mv.reviewed_at, mv.locked_at
              from mission_versions mv
             where mv.mission_id = mi.id and (mi.created_by = $1 or mv.reviewed_at is not null)
             order by mv.version_number desc limit 1) v on true
     where mi.school_id = $2 and kb.archived_at is null
       and (($8 = 'archived' and mi.archived_at is not null)
            or ($8 is distinct from 'archived' and mi.archived_at is null))
       and ($3::uuid is null or kb.school_subject_id = $3)
       and (mi.created_by = $1 or (v.id is not null and exists (
             select 1 from teaching_assignments ta
              where ta.school_id = mi.school_id and ta.teacher_id = $1
                and ta.school_subject_id = kb.school_subject_id)))
       and ($5::timestamptz is null or (mi.created_at, mi.id) < ($5, $6))
       and ($7 = '' or position(lower($7) in lower(mi.title)) > 0)
       and ($8::text is null or $8 = 'archived' or $8 = case
              when v.locked_at is not null then 'locked'
              when v.reviewed_at is not null then 'reviewed' else 'draft' end)
     order by mi.created_at desc, mi.id desc
     limit $4
"""


class PgMissionsRepo:
    async def version_history(
        self, mission_id: UUID, include_drafts: bool
    ) -> list[VersionHistoryEntry]:
        rows = await self._conn.fetch(
            "select mv.version_number, mv.created_at, pr.full_name as created_by_name,"
            " mv.reviewed_at, mv.locked_at from mission_versions mv"
            " left join profiles pr on pr.id = mv.created_by where mv.mission_id = $1"
            " and ($2 or mv.reviewed_at is not null or mv.locked_at is not null)"
            " order by mv.version_number desc",
            mission_id,
            include_drafts,
        )
        return [
            VersionHistoryEntry(**dict(r), status=version_status(r["reviewed_at"], r["locked_at"]))
            for r in rows
        ]

    def __init__(self, conn: DbConnection) -> None:
        self._conn = conn

    async def active_generation_job(self, mission_id: UUID) -> UUID | None:
        await self._conn.execute("select id from missions where id = $1 for update", mission_id)
        job_id: UUID | None = await self._conn.fetchval(
            "select id from jobs where entity_id = $1 and entity_type = 'missions'"
            " and kind = 'mission_generate' and status in ('queued', 'running')"
            " order by created_at desc limit 1",
            mission_id,
        )
        return job_id

    async def generation_catalog(self, mission: MissionRef) -> GenerationCatalog:
        objective: str = await self._conn.fetchval(
            "select learning_objective from missions where id = $1",
            mission.id,
        )
        concepts = await self._conn.fetch(
            "select id, name, coalesce(description, '') as description, source_chunk_ids"
            " from concepts where knowledge_base_id = $1 and school_id = $2"
            " and review_status = 'approved' and archived_at is null order by created_at, id",
            mission.knowledge_base_id,
            mission.school_id,
        )
        wrong = await self._conn.fetch(
            "select m.id, m.concept_id, m.statement, m.correct_understanding,"
            " m.detection_cues, m.counter_examples, m.source_chunk_ids from misconceptions m"
            " join concepts c on c.id = m.concept_id"
            " where m.knowledge_base_id = $1 and m.school_id = $2"
            " and m.review_status = 'approved' and m.archived_at is null"
            " and c.review_status = 'approved' and c.archived_at is null"
            " order by m.created_at, m.id",
            mission.knowledge_base_id,
            mission.school_id,
        )
        return GenerationCatalog(
            objective,
            tuple(ApprovedConceptIn.model_validate(dict(c)) for c in concepts),
            tuple(ApprovedMisconceptionIn.model_validate(dict(m)) for m in wrong),
        )

    async def generation_paragraphs(
        self, mission: MissionRef, chunk_ids: Sequence[UUID]
    ) -> list[SourceParagraphIn]:
        rows = await self._conn.fetch(
            "select * from public.get_kb_chunks($1, $2, $3::uuid[])",
            mission.school_id,
            mission.knowledge_base_id,
            list(chunk_ids),
        )
        return [
            SourceParagraphIn.model_validate(
                {
                    "id": r["chunk_id"],
                    "kind": r["kind"],
                    "content": r["content"],
                    "heading_path": r["heading_path"] or "",
                    "page_start": r["page_start"],
                    "page_end": r["page_end"],
                }
            )
            for r in rows
        ]

    async def claim_generation(
        self, job_id: UUID, now: datetime, stale_after_s: float
    ) -> int | None:
        attempt: int | None = await self._conn.fetchval(
            "update jobs set status = 'running', attempts = attempts + 1 where id = $1"
            " and kind = 'mission_generate' and (status = 'queued'"
            " or (status = 'running' and updated_at < $2)) returning attempts",
            job_id,
            now - timedelta(seconds=stale_after_s),
        )
        return attempt

    async def touch_generation(self, job_id: UUID, attempt: int) -> bool:
        return (
            await self._conn.fetchval(
                "update jobs set updated_at = now() where id = $1 and attempts = $2"
                " and status = 'running' returning id",
                job_id,
                attempt,
            )
            is not None
        )

    async def checkpoint_generation(
        self, job_id: UUID, attempt: int, result: Mapping[str, Any], status: str = "running"
    ) -> bool:
        return (
            await self._conn.fetchval(
                "update jobs set result = $3::jsonb, status = $4::job_status where id = $1"
                " and attempts = $2 and status = 'running' returning id",
                job_id,
                attempt,
                json.dumps(dict(result)),
                status,
            )
            is not None
        )

    async def attach_generation_job(self, version_id: UUID, job_id: UUID) -> None:
        await self._conn.execute(
            "update mission_versions set generation_job_id = $2 where id = $1",
            version_id,
            job_id,
        )

    async def kb_school_id(self, kb_id: UUID) -> UUID | None:
        school_id: UUID | None = await self._conn.fetchval(
            "select school_id from knowledge_bases where id = $1", kb_id
        )
        return school_id

    async def mission_ref(self, mission_id: UUID) -> MissionRef | None:
        row = await self._conn.fetchrow(
            "select mi.id, mi.school_id, mi.knowledge_base_id, kb.school_subject_id, mi.created_by"
            "  from missions mi join knowledge_bases kb on kb.id = mi.knowledge_base_id"
            " where mi.id = $1 and mi.archived_at is null and kb.archived_at is null"
            " for no key update of kb, mi",
            mission_id,
        )
        return MissionRef(**dict(row)) if row else None

    async def create_mission(
        self, school_id: UUID, kb_id: UUID, actor_id: UUID, title: str, objective: str
    ) -> UUID:
        mission_id: UUID = await self._conn.fetchval(
            "insert into missions (school_id, knowledge_base_id, created_by, title,"
            " learning_objective) values ($1, $2, $3, $4, $5) returning id",
            school_id,
            kb_id,
            actor_id,
            title,
            objective,
        )
        return mission_id

    async def next_version_number(self, mission_id: UUID) -> int:
        await self._conn.execute("select 1 from missions where id = $1 for update", mission_id)
        number: int = await self._conn.fetchval(
            "select coalesce(max(version_number), 0) + 1 from mission_versions"
            " where mission_id = $1",
            mission_id,
        )
        return number

    async def item_problems(
        self,
        kb_id: UUID,
        school_id: UUID,
        concept_ids: Sequence[UUID],
        misconception_ids: Sequence[UUID],
        chunk_ids: Sequence[UUID],
    ) -> list[tuple[str, str]]:
        rows = await self._conn.fetch(
            """
            select 'ITEM_NOT_APPROVED' as code, i::text as id from unnest($2::uuid[]) i
             where not exists (select 1 from concepts c where c.id = i and c.knowledge_base_id = $1
                                 and c.review_status = 'approved' and c.archived_at is null)
            union all
            select 'ITEM_NOT_APPROVED', i::text from unnest($3::uuid[]) i
             where not exists (select 1 from misconceptions x where x.id = i
                                 and x.knowledge_base_id = $1 and x.review_status = 'approved'
                                 and x.archived_at is null)
            union all
            select 'SOURCE_CHUNK_UNKNOWN', i::text from unnest($4::uuid[]) i
             where not exists (select 1 from material_chunks m where m.id = i
                                 and m.knowledge_base_id = $1 and m.school_id = $5)
            """,
            kb_id,
            list(concept_ids),
            list(misconception_ids),
            list(chunk_ids),
            school_id,
        )
        return [(r["code"], r["id"]) for r in rows]

    async def insert_version(
        self,
        mission: MissionRef,
        number: int,
        actor_id: UUID,
        draft: VersionDraft,
        probe_plan: Mapping[str, Sequence[str]],
    ) -> UUID:
        version_id: UUID = await self._conn.fetchval(
            "insert into mission_versions (school_id, mission_id, version_number, anchor_problem,"
            " rubric, probe_plan, max_turns, max_duration_minutes, created_by, reference_reasoning,"
            " question_bank, answer_terms, live_warmup, source_chunk_ids)"
            " values ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7, $8, $9, $10, $11::jsonb, $12,"
            " $13::jsonb, $14) returning id",
            mission.school_id,
            mission.id,
            number,
            draft.anchor_problem,
            json.dumps({k: list(v) for k, v in draft.rubric.items()}),
            json.dumps({k: list(v) for k, v in probe_plan.items()}),
            draft.max_turns,
            draft.max_duration_minutes,
            actor_id,
            draft.reference_reasoning,
            json.dumps([dict(q) for q in draft.question_bank]),
            list(draft.answer_terms),
            json.dumps(draft.live_warmup) if draft.live_warmup is not None else None,
            list(draft.source_chunk_ids),
        )
        await self._conn.executemany(
            "insert into mission_version_concepts (mission_version_id, concept_id) values ($1, $2)",
            [(version_id, c) for c in draft.target_concept_ids],
        )
        await self._conn.executemany(
            "insert into mission_version_misconceptions (mission_version_id, misconception_id)"
            " values ($1, $2)",
            [(version_id, m) for m in draft.misconception_ids],
        )
        return version_id

    async def version(self, mission_id: UUID, number: int) -> VersionRecord | None:
        row = await self._conn.fetchrow(
            """
            select mv.*,
                   array(select concept_id from mission_version_concepts
                          where mission_version_id = mv.id) as target_concept_ids,
                   array(select misconception_id from mission_version_misconceptions
                          where mission_version_id = mv.id) as misconception_ids
              from mission_versions mv where mv.mission_id = $1 and mv.version_number = $2
            """,
            mission_id,
            number,
        )
        if row is None:
            return None
        draft = VersionDraft(
            anchor_problem=row["anchor_problem"],
            rubric=json.loads(row["rubric"]),
            target_concept_ids=tuple(row["target_concept_ids"]),
            misconception_ids=tuple(row["misconception_ids"]),
            question_bank=tuple(json.loads(row["question_bank"])),
            answer_terms=tuple(row["answer_terms"]),
            reference_reasoning=row["reference_reasoning"] or "",
            source_chunk_ids=tuple(row["source_chunk_ids"]),
            live_warmup=json.loads(row["live_warmup"]) if row["live_warmup"] else None,
            max_turns=row["max_turns"],
            max_duration_minutes=row["max_duration_minutes"],
        )
        return VersionRecord(
            id=row["id"],
            mission_id=row["mission_id"],
            version_number=row["version_number"],
            status=version_status(row["reviewed_at"], row["locked_at"]),
            draft=draft,
            created_by=row["created_by"],
            reviewed_at=row["reviewed_at"],
        )

    async def version_items(
        self, version_id: UUID
    ) -> tuple[tuple[Target, ...], tuple[PackMisconception, ...]]:
        concepts = await self._conn.fetch(
            "select c.id, c.name, coalesce(c.description, '') as description"
            "  from mission_version_concepts vc join concepts c on c.id = vc.concept_id"
            " where vc.mission_version_id = $1 and c.review_status = 'approved'"
            "   and c.archived_at is null order by c.created_at",
            version_id,
        )
        misconceptions = await self._conn.fetch(
            "select x.id, x.concept_id, x.statement, x.detection_cues"
            "  from mission_version_misconceptions vm join misconceptions x"
            "    on x.id = vm.misconception_id"
            " where vm.mission_version_id = $1 and x.review_status = 'approved'"
            "   and x.archived_at is null order by x.created_at",
            version_id,
        )
        return (
            tuple(Target(c["id"], c["name"], c["description"]) for c in concepts),
            tuple(
                PackMisconception(
                    m["id"], m["concept_id"], m["statement"], tuple(m["detection_cues"])
                )
                for m in misconceptions
            ),
        )

    async def mark_reviewed(
        self, version_id: UUID, pack: Mapping[str, Any], actor_id: UUID, now: datetime
    ) -> bool:
        # AI-10 / invariant 10: context_pack is frozen here, before any publication locks the row.
        return (
            await self._conn.fetchval(
                "update mission_versions set context_pack = $2::jsonb, reviewed_at = $3,"
                " reviewed_by = $4 where id = $1 and reviewed_at is null and locked_at is null"
                " returning id",
                version_id,
                json.dumps(pack),
                now,
                actor_id,
            )
            is not None
        )

    async def mission_page(
        self,
        actor_id: UUID,
        school_id: UUID,
        school_subject_id: UUID | None,
        limit: int,
        after: tuple[datetime, UUID] | None,
        search: str = "",
        status: str | None = None,
    ) -> list[MissionSummary]:
        rows = await self._conn.fetch(
            _MISSION_PAGE,
            actor_id,
            school_id,
            school_subject_id,
            limit,
            after[0] if after else None,
            after[1] if after else None,
            search.strip(),
            status,
        )
        return [
            MissionSummary(
                id=r["id"],
                title=r["title"],
                knowledge_base_id=r["knowledge_base_id"],
                created_by=r["created_by"],
                created_by_name=r["created_by_name"],
                latest_version=VersionSummary(
                    r["version_id"],
                    r["version_number"],
                    version_status(r["reviewed_at"], r["locked_at"]),
                )
                if r["version_id"]
                else None,
                created_at=r["created_at"],
            )
            for r in rows
        ]

    async def archive(self, mission_id: UUID, now: datetime) -> UUID:
        row = await self._conn.fetchrow(
            "select school_id from missions where id = $1 for update", mission_id
        )
        if row is None:
            raise NotFound()
        if await self._conn.fetchval(
            "select exists(select 1 from jobs where entity_type = 'missions' and entity_id = $1"
            " and status in ('queued','running'))",
            mission_id,
        ):
            raise Conflict("MISSION_BUSY")
        await self._conn.execute(
            "update missions set archived_at = coalesce(archived_at, $2) where id = $1",
            mission_id,
            now,
        )
        return UUID(str(row["school_id"]))
