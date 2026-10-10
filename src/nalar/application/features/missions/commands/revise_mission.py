from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any
from uuid import UUID

from nalar.application.errors import Conflict, DependencyUnavailable, NotFound, Unprocessable
from nalar.application.features.missions.commands.create_version import require_creator
from nalar.application.ports.ai_contract import (
    ContextPackIn,
    GenerateMissionIn,
    GenerateMissionOut,
    ReviseMissionIn,
    RevisionBase,
)
from nalar.application.ports.missions import (
    MissionRef,
    MissionRevisionPolicy,
    RevisionRequestRecord,
    VersionRecord,
)
from nalar.application.ports.queue import DEFAULT_QUEUE
from nalar.application.ports.uow import UnitOfWork
from nalar.domain.context_pack import (
    PackMisconception,
    Target,
    VersionContent,
    build_context_pack,
    derive_probe_plan,
    question_bank_from_pack,
)
from nalar.domain.mission_revisions import effective_scope, request_hash

REVISION_KIND = "mission_revise"


@dataclass(frozen=True)
class RevisionQueued:
    job_id: UUID
    status: str
    base_version_id: UUID
    effective_scope: tuple[str, ...]


async def revision_input(
    uow: UnitOfWork,
    mission: MissionRef,
    base: VersionRecord,
    intent: Mapping[str, Any],
    scope: list[str],
) -> ReviseMissionIn:
    d = base.draft
    catalog = await uow.missions.generation_catalog(mission)
    by_id = {c.id: c for c in catalog.concepts}
    chosen = [UUID(str(i)) for i in intent.get("target_concept_ids", d.target_concept_ids)]
    if len(set(chosen)) != len(chosen) or any(
        c not in by_id for c in (*chosen, *d.target_concept_ids)
    ):
        raise Unprocessable("ITEM_NOT_APPROVED")
    targets = [by_id[c] for c in chosen]
    old_wrong = [m for m in catalog.misconceptions if m.id in d.misconception_ids]
    if len(old_wrong) != len(d.misconception_ids):
        raise Unprocessable("ITEM_NOT_APPROVED")
    wrong = (
        [m for m in catalog.misconceptions if m.concept_id in chosen]
        if "anchor_problem" in scope
        else old_wrong
    )
    source_ids = list(
        dict.fromkeys(
            [
                *d.source_chunk_ids,
                *(p for c in targets for p in c.source_chunk_ids or []),
                *(p for m in wrong for p in m.source_chunk_ids or []),
            ]
        )
    )
    paragraphs = await uow.missions.generation_paragraphs(mission, source_ids)
    if not set(d.source_chunk_ids) <= {p.id for p in paragraphs}:
        raise Unprocessable("MISSION_SOURCES_CHANGED")
    content = VersionContent(
        d.anchor_problem,
        d.reference_reasoning,
        d.rubric,
        tuple(
            Target(by_id[c].id, by_id[c].name, by_id[c].description or "")
            for c in d.target_concept_ids
        ),
        tuple(
            PackMisconception(
                m.id, m.concept_id, m.statement, tuple(c.root for c in m.detection_cues or [])
            )
            for m in old_wrong
        ),
        question_bank_from_pack({"question_bank": d.question_bank}),
        d.answer_terms,
        d.max_turns,
        d.max_duration_minutes,
    )
    old_generation = GenerateMissionOut(
        context_pack=ContextPackIn.model_validate(build_context_pack(content)),
        rubric=d.rubric,
        source_chunk_ids=list(d.source_chunk_ids),
        ungrounded_concept_ids=[],
        probe_plan=derive_probe_plan(content.question_bank),
    )
    return ReviseMissionIn(
        base=RevisionBase(
            learning_objective=d.learning_objective or catalog.learning_objective,
            generation=old_generation,
        ),
        generation_input=GenerateMissionIn(
            learning_objective=intent.get(
                "learning_objective", d.learning_objective or catalog.learning_objective
            ),
            targets=targets,
            misconceptions=wrong,
            paragraphs=paragraphs,
            max_probes=d.max_turns,
            max_duration_minutes=d.max_duration_minutes,
        ),
        feedback=intent.get("feedback", []),
    )


class ReviseMissionHandler:
    def __init__(self, uow: UnitOfWork, policy: MissionRevisionPolicy) -> None:
        self._uow, self._policy = uow, policy

    async def execute(
        self, actor_id: UUID, mission_id: UUID, intent: Mapping[str, Any], key: UUID
    ) -> RevisionQueued:
        digest = request_hash(intent)
        async with self._uow:
            mission = await require_creator(self._uow, actor_id, mission_id)
            if not await self._uow.authz.can_read_kb(actor_id, mission.knowledge_base_id):
                raise NotFound()
            replay = await self._uow.missions.revision_by_key(actor_id, mission_id, key)
            if replay is not None:
                if replay.request_hash != digest:
                    raise Conflict("IDEMPOTENCY_KEY_REUSED")
                job = await self._uow.jobs.get(replay.job_id)
                assert job is not None
                return RevisionQueued(
                    job.id,
                    job.status,
                    UUID(str(replay.request["base_version_id"])),
                    tuple(replay.request["scope"]),
                )
            if not self._policy.enabled:
                raise DependencyUnavailable("MISSION_REVISION_UNAVAILABLE")
            if await self._uow.missions.active_generation_job(mission_id) is not None:
                raise Conflict("REVISION_IN_PROGRESS")
            latest = await self._uow.missions.latest_version_id(mission_id)
            if str(latest) != str(intent["expected_latest_version_id"]):
                raise Conflict("MISSION_VERSION_CHANGED")
            base = await self._uow.missions.version_by_id(
                mission_id, UUID(str(intent["base_version_id"]))
            )
            if base is None:
                raise NotFound()
            d = base.draft
            feedback = list(intent.get("feedback", []))
            if len({f["id"] for f in feedback}) != len(feedback):
                raise Unprocessable("REVISION_FEEDBACK_INVALID")
            qids = {str(q["id"]) for q in d.question_bank}
            if any(not set(f.get("question_ids", [])) <= qids for f in feedback):
                raise Unprocessable("REVISION_QUESTIONS_UNKNOWN")
            goal = intent.get("learning_objective", d.learning_objective) != d.learning_objective
            goal = goal or list(
                map(str, intent.get("target_concept_ids", d.target_concept_ids))
            ) != list(map(str, d.target_concept_ids))
            scope = effective_scope(goal, feedback)
            title = intent.get("title", d.title)
            if not scope and title == d.title:
                raise Unprocessable("REVISION_NO_CHANGE")
            if scope and (
                not 4 <= d.max_turns <= 6
                or not 5 <= d.max_duration_minutes <= 20
                or not 2 <= len(intent.get("target_concept_ids", d.target_concept_ids)) <= 3
            ):
                raise Unprocessable("REVISION_SETTINGS_UNSUPPORTED")
            try:
                ai_input = (
                    await revision_input(self._uow, mission, base, intent, scope) if scope else None
                )
            except ValueError as exc:
                raise Unprocessable("REVISION_BASE_INVALID") from exc
            problems = await self._uow.missions.item_problems(
                mission.knowledge_base_id,
                mission.school_id,
                d.target_concept_ids,
                d.misconception_ids,
                d.source_chunk_ids,
            )
            if problems:
                raise Unprocessable("ITEM_NOT_APPROVED")
            job_id = await self._uow.jobs.create(
                kind=REVISION_KIND,
                entity_type="missions",
                entity_id=mission_id,
                school_id=mission.school_id,
                requested_by=actor_id,
            )
            stored = {
                "intent": dict(intent),
                "scope": scope,
                "base_version_id": str(base.id),
                "expected_latest_version_id": str(latest),
                "base_draft": asdict(d),
                "title": title,
                "ai_input": ai_input.model_dump(mode="json") if ai_input else None,
            }
            await self._uow.missions.create_revision_request(
                mission, actor_id, key, RevisionRequestRecord(job_id, digest, stored)
            )
            await self._uow.queue.send(
                DEFAULT_QUEUE,
                {"kind": REVISION_KIND, "mission_id": str(mission_id), "job_id": str(job_id)},
            )
        return RevisionQueued(job_id, "queued", base.id, tuple(scope))
