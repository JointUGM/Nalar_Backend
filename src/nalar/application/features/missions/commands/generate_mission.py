import hashlib
import json
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from nalar.application.errors import AppError, Conflict, NotFound, Unprocessable
from nalar.application.features.missions.commands.create_version import require_creator
from nalar.application.ports.ai import AiGateway, AiResult, AiServiceError
from nalar.application.ports.ai_contract import (
    GenerateMissionIn,
    GenerateMissionOut,
    InvocationOut,
    SelectTargetsIn,
)
from nalar.application.ports.clock import Clock
from nalar.application.ports.missions import MissionRef, VersionDraft
from nalar.application.ports.queue import DEFAULT_QUEUE
from nalar.application.ports.uow import UnitOfWork
from nalar.domain.context_pack import (
    PackMisconception,
    Target,
    VersionContent,
    derive_probe_plan,
    question_bank_from_pack,
    validate_version,
)
from nalar.domain.labels import PROBE_MOVES

GENERATION_KIND = "mission_generate"
LEASE_SECONDS = 600.0
MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class GenerationQueued:
    job_id: UUID


class GenerateMissionHandler:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, actor_id: UUID, mission_id: UUID) -> GenerationQueued:
        async with self._uow:
            mission = await require_creator(self._uow, actor_id, mission_id)
            if not await self._uow.authz.can_read_kb(actor_id, mission.knowledge_base_id):
                raise NotFound()
            existing = await self._uow.missions.active_generation_job(mission_id)
            if existing is not None:
                active = await self._uow.jobs.get(existing)
                if active is not None and active.kind != GENERATION_KIND:
                    raise Conflict("REVISION_IN_PROGRESS")
                return GenerationQueued(existing)
            catalog = await self._uow.missions.generation_catalog(mission)
            if not 2 <= len(catalog.concepts) <= 200:
                raise Unprocessable(
                    "MISSION_TARGETS_UNAVAILABLE",
                    "Misi memerlukan sedikitnya dua konsep yang disetujui.",
                )
            job_id = await self._uow.jobs.create(
                kind=GENERATION_KIND,
                entity_type="missions",
                entity_id=mission_id,
                school_id=mission.school_id,
                requested_by=actor_id,
            )
            await self._uow.queue.send(
                DEFAULT_QUEUE,
                {
                    "kind": GENERATION_KIND,
                    "mission_id": str(mission_id),
                    "job_id": str(job_id),
                },
            )
        return GenerationQueued(job_id)


class GenerationSuperseded(Exception):
    pass


class GenerationInvalid(Exception):
    pass


class BuildMissionHandler:
    def __init__(self, uow: UnitOfWork, ai: AiGateway, clock: Clock) -> None:
        self._uow, self._ai, self._clock = uow, ai, clock

    async def execute(self, mission_id: UUID, job_id: UUID) -> None:
        async with self._uow:
            job = await self._uow.jobs.get(job_id)
            if (
                job is None
                or job.kind != GENERATION_KIND
                or job.entity_type != "missions"
                or job.entity_id != mission_id
                or job.requested_by is None
                or job.status not in ("queued", "running")
            ):
                return
            attempt = await self._uow.missions.claim_generation(
                job_id, self._clock.now(), LEASE_SECONDS
            )
            if attempt is None:
                return
        self._job_id, self._attempt, self._actor_id, self._mission_id = (
            job_id,
            attempt,
            job.requested_by,
            mission_id,
        )
        checkpoint: dict[str, Any] = dict(job.result or {})
        try:
            async with self._uow:
                mission = await self._guard()
                catalog = await self._uow.missions.generation_catalog(mission)
            concepts = {c.id: c for c in catalog.concepts}
            if not 2 <= len(concepts) <= 200:
                raise GenerationInvalid("MISSION_TARGETS_UNAVAILABLE")
            catalog_key = hashlib.sha256(
                json.dumps(
                    {
                        "objective": catalog.learning_objective,
                        "concepts": [c.model_dump(mode="json") for c in catalog.concepts],
                        "misconceptions": [
                            m.model_dump(mode="json") for m in catalog.misconceptions
                        ],
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            if "target_concept_ids" in checkpoint and checkpoint.get("catalog_key") != catalog_key:
                raise GenerationInvalid("MISSION_ITEMS_CHANGED")
            if "target_concept_ids" not in checkpoint:
                selected = await self._call(
                    lambda rid: self._ai.select_targets(
                        SelectTargetsIn(
                            learning_objective=catalog.learning_objective,
                            concepts=list(catalog.concepts),
                        ),
                        rid,
                    ),
                    mission,
                    f"mission-{job_id}-{attempt}-select",
                )
                checkpoint["target_concept_ids"] = [
                    str(i) for i in selected.result.target_concept_ids
                ]
                checkpoint["catalog_key"] = catalog_key
                async with self._uow:
                    await self._guard()
                    await self._uow.missions.checkpoint_generation(job_id, attempt, checkpoint)
            selected_ids = [UUID(str(i)) for i in checkpoint["target_concept_ids"]]
            if (
                not 2 <= len(selected_ids) <= 3
                or len(set(selected_ids)) != len(selected_ids)
                or any(i not in concepts for i in selected_ids)
            ):
                raise GenerationInvalid("MISSION_TARGET_SELECTION_INVALID")
            targets = [concepts[i] for i in selected_ids]
            wrong = [m for m in catalog.misconceptions if m.concept_id in selected_ids]
            source_ids = list(
                dict.fromkeys(
                    [
                        *(p for c in targets for p in c.source_chunk_ids or []),
                        *(p for m in wrong for p in m.source_chunk_ids or []),
                    ]
                )
            )
            async with self._uow:
                await self._guard()
                paragraphs = await self._uow.missions.generation_paragraphs(mission, source_ids)
            request = GenerateMissionIn(
                learning_objective=catalog.learning_objective,
                targets=targets,
                misconceptions=wrong,
                paragraphs=paragraphs,
                max_probes=6,
                max_duration_minutes=20,
            )
            if "generated" in checkpoint:
                request_key = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
                if checkpoint.get("request_key") != request_key:
                    raise GenerationInvalid("MISSION_SOURCES_CHANGED")
                generated = GenerateMissionOut.model_validate(checkpoint["generated"])
            else:
                reply = await self._call(
                    lambda rid: self._ai.generate_mission(request, rid),
                    mission,
                    f"mission-{job_id}-{attempt}-generate",
                )
                generated = reply.result
                checkpoint["generated"] = generated.model_dump(mode="json")
                checkpoint["request_key"] = hashlib.sha256(
                    request.model_dump_json().encode()
                ).hexdigest()
                async with self._uow:
                    await self._guard()
                    await self._uow.missions.checkpoint_generation(job_id, attempt, checkpoint)
            draft = self._draft(
                generated, selected_ids, [m.id for m in wrong], {p.id for p in paragraphs}
            )
            async with self._uow:
                await self._guard()
                fresh = await self._uow.missions.generation_catalog(mission)
                if fresh != catalog:
                    raise GenerationInvalid("MISSION_ITEMS_CHANGED")
                available = await self._uow.missions.generation_paragraphs(
                    mission, draft.source_chunk_ids
                )
                if {p.id for p in available} != set(draft.source_chunk_ids):
                    raise GenerationInvalid("MISSION_SOURCES_CHANGED")
                problems = await self._uow.missions.item_problems(
                    mission.knowledge_base_id,
                    mission.school_id,
                    draft.target_concept_ids,
                    draft.misconception_ids,
                    draft.source_chunk_ids,
                )
                if problems:
                    raise GenerationInvalid("MISSION_ITEMS_CHANGED")
                number = await self._uow.missions.next_version_number(mission.id)
                version_id = await self._uow.missions.insert_version(
                    mission,
                    number,
                    self._actor_id,
                    draft,
                    derive_probe_plan(
                        question_bank_from_pack({"question_bank": draft.question_bank})
                    ),
                )
                await self._uow.missions.attach_generation_job(version_id, job_id)
                await self._uow.missions.checkpoint_generation(
                    job_id,
                    attempt,
                    {
                        "version_id": str(version_id),
                        "version_number": number,
                        "ungrounded_concept_ids": [
                            str(i) for i in generated.ungrounded_concept_ids
                        ],
                    },
                    "succeeded",
                )
        except GenerationSuperseded:
            return
        except AiServiceError as error:
            transient = error.http_status == 503 or error.code in ("timeout", "unreachable")
            retry = transient and attempt < MAX_ATTEMPTS
            async with self._uow:
                if await self._uow.missions.touch_generation(job_id, attempt):
                    if retry:
                        await self._uow.missions.checkpoint_generation(
                            job_id, attempt, checkpoint, "queued"
                        )
                    else:
                        await self._uow.jobs.mark_failed(
                            job_id, error.code, "Pembuatan misi gagal. Silakan coba lagi."
                        )
            if retry:
                raise
        except (GenerationInvalid, AppError, ValueError) as error:
            async with self._uow:
                if await self._uow.missions.touch_generation(job_id, attempt):
                    code = (
                        str(error)
                        if isinstance(error, GenerationInvalid)
                        else "MISSION_GENERATION_INVALID"
                    )
                    await self._uow.jobs.mark_failed(
                        job_id, code, "Draf tidak disimpan. Periksa konsep dan akses Anda."
                    )

    async def _guard(self) -> MissionRef:
        if not await self._uow.missions.touch_generation(self._job_id, self._attempt):
            raise GenerationSuperseded()
        mission = await require_creator(self._uow, self._actor_id, self._mission_id)
        if not await self._uow.authz.can_read_kb(self._actor_id, mission.knowledge_base_id):
            raise NotFound()
        return mission

    async def _record(self, school_id: UUID, invocations: Sequence[InvocationOut]) -> None:
        async with self._uow:
            await self._uow.ai_invocations.record(school_id, invocations)

    async def _call[T](
        self, call: Callable[[str], Awaitable[AiResult[T]]], mission: MissionRef, request_id: str
    ) -> AiResult[T]:
        try:
            reply = await call(request_id)
        except AiServiceError as error:
            await self._record(mission.school_id, error.invocations)
            raise
        await self._record(mission.school_id, reply.invocations)
        return reply

    def _draft(
        self,
        output: GenerateMissionOut,
        selected: list[UUID],
        wrong: list[UUID],
        sources: set[UUID],
    ) -> VersionDraft:
        pack = output.context_pack
        if (
            {t.id for t in pack.targets} != set(selected)
            or {m.id for m in pack.misconceptions or []} != set(wrong)
            or not set(output.source_chunk_ids) <= sources
            or not set(output.ungrounded_concept_ids) <= set(selected)
        ):
            raise GenerationInvalid("MISSION_OUTPUT_REFERENCES_INVALID")
        content = VersionContent(
            anchor_problem=pack.anchor_problem,
            reference_reasoning=pack.reference_reasoning,
            rubric=output.rubric.model_dump(),
            targets=tuple(Target(t.id, t.name, t.description or "") for t in pack.targets),
            misconceptions=tuple(
                PackMisconception(
                    m.id, m.concept_id, m.statement, tuple(c.root for c in m.detection_cues or [])
                )
                for m in pack.misconceptions or []
            ),
            question_bank=question_bank_from_pack(pack.model_dump(mode="json")),
            answer_terms=tuple(t.root for t in pack.answer_terms),
            max_turns=pack.max_probes,
            max_duration_minutes=pack.max_duration_minutes,
        )
        counts = Counter((q.concept_id, q.move) for q in content.question_bank)
        if (
            validate_version(content)
            or not 4 <= pack.max_probes <= 6
            or not 5 <= pack.max_duration_minutes <= 20
            or any(counts[(c, move)] < 2 for c in selected for move in PROBE_MOVES)
        ):
            raise GenerationInvalid("MISSION_OUTPUT_INVALID")
        return VersionDraft(
            content.anchor_problem,
            content.rubric,
            tuple(selected),
            tuple(wrong),
            tuple(q.model_dump(mode="json") for q in pack.question_bank),
            content.answer_terms,
            content.reference_reasoning,
            tuple(output.source_chunk_ids),
            None,
            content.max_turns,
            content.max_duration_minutes,
        )
