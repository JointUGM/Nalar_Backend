from dataclasses import asdict, replace
from uuid import UUID

from nalar.application.errors import AppError
from nalar.application.features.missions.commands.generate_mission import (
    LEASE_SECONDS,
    MAX_ATTEMPTS,
    BuildMissionHandler,
    GenerationInvalid,
    GenerationSuperseded,
)
from nalar.application.features.missions.commands.revise_mission import (
    REVISION_KIND,
    revision_input,
)
from nalar.application.ports.ai import AiServiceError
from nalar.application.ports.ai_contract import ReviseMissionIn, ReviseMissionOut
from nalar.domain.context_pack import derive_probe_plan, question_bank_from_pack
from nalar.domain.mission_revisions import changed_fields, request_hash


class BuildRevisionHandler(BuildMissionHandler):
    async def execute(self, mission_id: UUID, job_id: UUID) -> None:
        async with self._uow:
            job = await self._uow.jobs.get(job_id)
            if (
                job is None
                or job.kind != REVISION_KIND
                or job.entity_id != mission_id
                or job.entity_type != "missions"
                or job.requested_by is None
                or job.status not in ("queued", "running")
            ):
                return
            attempt = await self._uow.missions.claim_generation(
                job_id, self._clock.now(), LEASE_SECONDS
            )
            if attempt is None:
                return
            record = await self._uow.missions.revision_request(job_id)
        self._job_id, self._attempt, self._actor_id, self._mission_id = (
            job_id,
            attempt,
            job.requested_by,
            mission_id,
        )
        checkpoint = dict(job.result or {})
        try:
            if record is None:
                raise GenerationInvalid("REVISION_REQUEST_MISSING")
            stored = record.request
            async with self._uow:
                mission = await self._guard()
                base = await self._uow.missions.version_by_id(
                    mission_id, UUID(str(stored["base_version_id"]))
                )
                if base is None:
                    raise GenerationInvalid("MISSION_VERSION_CHANGED")
                if request_hash(asdict(base.draft)) != request_hash(stored["base_draft"]):
                    raise GenerationInvalid("MISSION_VERSION_CHANGED")
                if (
                    str(await self._uow.missions.latest_version_id(mission_id))
                    != stored["expected_latest_version_id"]
                ):
                    raise GenerationInvalid("MISSION_VERSION_CHANGED")
                current = (
                    await revision_input(
                        self._uow, mission, base, stored["intent"], list(stored["scope"])
                    )
                    if stored["scope"]
                    else None
                )
            request = (
                ReviseMissionIn.model_validate(stored["ai_input"]) if stored["ai_input"] else None
            )
            if current != request:
                raise GenerationInvalid("MISSION_ITEMS_CHANGED")
            if request is None:
                draft = replace(base.draft, title=str(stored["title"]), base_version_id=base.id)
                warnings: list[UUID] = []
            else:
                if "generated" in checkpoint:
                    generated = ReviseMissionOut.model_validate(checkpoint["generated"])
                else:
                    reply = await self._call(
                        lambda rid: self._ai.revise_mission(request, rid),
                        mission,
                        f"revision-{job_id}-{attempt}",
                    )
                    generated = reply.result
                    checkpoint["generated"] = generated.model_dump(mode="json")
                    async with self._uow:
                        await self._guard()
                        await self._uow.missions.checkpoint_generation(job_id, attempt, checkpoint)
                if generated.effective_scope != list(stored["scope"]):
                    raise GenerationInvalid("REVISION_SCOPE_CONFLICT")
                if (
                    generated.generation.context_pack.max_probes
                    != request.generation_input.max_probes
                    or generated.generation.context_pack.max_duration_minutes
                    != request.generation_input.max_duration_minutes
                ):
                    raise GenerationInvalid("REVISION_SETTINGS_CHANGED")
                old = request.base.generation.model_dump(mode="json")
                new = generated.generation.model_dump(mode="json")
                if "anchor_problem" not in stored["scope"]:
                    old_cmp, new_cmp = dict(old), dict(new)
                    if "rubric" in stored["scope"]:
                        old_cmp.pop("rubric")
                        new_cmp.pop("rubric")
                    if "bank" in stored["scope"]:
                        old_cmp["context_pack"] = dict(old_cmp["context_pack"])
                        new_cmp["context_pack"] = dict(new_cmp["context_pack"])
                        old_bank = old_cmp["context_pack"].pop("question_bank")
                        new_bank = new_cmp["context_pack"].pop("question_bank")
                        feedback = [
                            f
                            for f in stored["intent"].get("feedback", [])
                            if f["component"] == "bank"
                        ]
                        allowed = (
                            {q["id"] for q in old_bank}
                            if any(not f.get("question_ids") for f in feedback)
                            else {id_ for f in feedback for id_ in f.get("question_ids", [])}
                        )
                        if len(old_bank) != len(new_bank):
                            raise GenerationInvalid("REVISION_SCOPE_CONFLICT")
                        for previous, changed in zip(old_bank, new_bank, strict=True):
                            expected = (
                                {**previous, "text": changed["text"]}
                                if previous["id"] in allowed
                                else previous
                            )
                            if expected != changed:
                                raise GenerationInvalid("REVISION_SCOPE_CONFLICT")
                    if old_cmp != new_cmp:
                        raise GenerationInvalid("REVISION_SCOPE_CONFLICT")
                inp = request.generation_input
                draft = self._draft(
                    generated.generation,
                    [t.id for t in inp.targets],
                    [m.id for m in inp.misconceptions or []],
                    {p.id for p in inp.paragraphs or []},
                )
                draft = replace(
                    draft,
                    learning_objective=inp.learning_objective,
                    title=str(stored["title"]),
                    base_version_id=base.id,
                    live_warmup=None
                    if "anchor_problem" in stored["scope"]
                    else base.draft.live_warmup,
                )
                warnings = list(generated.generation.ungrounded_concept_ids)
            async with self._uow:
                await self._guard()
                if (
                    str(await self._uow.missions.latest_version_id(mission_id))
                    != stored["expected_latest_version_id"]
                ):
                    raise GenerationInvalid("MISSION_VERSION_CHANGED")
                fresh_base = await self._uow.missions.version_by_id(mission_id, base.id)
                if fresh_base is None or fresh_base.draft != base.draft:
                    raise GenerationInvalid("MISSION_VERSION_CHANGED")
                fresh = (
                    await revision_input(
                        self._uow, mission, base, stored["intent"], list(stored["scope"])
                    )
                    if request
                    else None
                )
                if fresh != request:
                    raise GenerationInvalid("MISSION_ITEMS_CHANGED")
                problems = await self._uow.missions.item_problems(
                    mission.knowledge_base_id,
                    mission.school_id,
                    draft.target_concept_ids,
                    draft.misconception_ids,
                    draft.source_chunk_ids,
                )
                if problems:
                    raise GenerationInvalid("MISSION_ITEMS_CHANGED")
                number = await self._uow.missions.next_version_number(mission_id)
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
                        "ungrounded_concept_ids": [str(i) for i in warnings],
                        "changed_fields": changed_fields(asdict(base.draft), asdict(draft)),
                    },
                    "succeeded",
                )
        except GenerationSuperseded:
            return
        except AiServiceError as error:
            retry = (
                error.http_status == 503 or error.code in ("timeout", "unreachable")
            ) and attempt < MAX_ATTEMPTS
            async with self._uow:
                if await self._uow.missions.touch_generation(job_id, attempt):
                    if retry:
                        await self._uow.missions.checkpoint_generation(
                            job_id, attempt, checkpoint, "queued"
                        )
                    else:
                        await self._uow.jobs.mark_failed(
                            job_id,
                            error.code,
                            "Revisi gagal. Versi sebelumnya tetap dapat digunakan.",
                        )
            if retry:
                raise
        except (GenerationInvalid, AppError, ValueError) as error:
            async with self._uow:
                if await self._uow.missions.touch_generation(job_id, attempt):
                    code = (
                        str(error)
                        if isinstance(error, GenerationInvalid)
                        else (error.code if isinstance(error, AppError) else "REVISION_INVALID")
                    )
                    await self._uow.jobs.mark_failed(
                        job_id,
                        code,
                        "Draf revisi tidak disimpan. Periksa tujuan, konsep, dan versi dasar.",
                    )
