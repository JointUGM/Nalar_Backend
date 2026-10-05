from uuid import UUID

from nalar.application.errors import Conflict, InvalidInput, NotFound
from nalar.application.features.administration.commands.request_digest import request_digest
from nalar.application.features.administration.commands.upload_reference import (
    EXTRACT_REFERENCE,
    INDEX_REFERENCE,
    queue_reference,
    require_platform,
)
from nalar.application.ports.national_references import ReferencePolicy, ReferenceRow
from nalar.application.ports.uow import UnitOfWork
from nalar.domain.national_references import normalized, verify_source


class ReviewReferenceHandler:
    def __init__(self, uow: UnitOfWork, policy: ReferencePolicy) -> None:
        self._uow = uow
        self._policy = policy

    async def execute(
        self,
        actor_id: UUID,
        document_id: UUID,
        revision: int,
        draft: ReferenceRow,
    ) -> int:
        async with self._uow:
            await require_platform(self._uow, actor_id)
            row = await self._uow.national_references.get(document_id, lock=True)
            if row is None:
                raise NotFound()
            if row["status"] not in ("review", "failed") or row["revision"] != revision:
                raise Conflict("REFERENCE_REVISION_CONFLICT")
            pages = await self._uow.national_references.pages(document_id)
            try:
                if row["kind"] == "curriculum":
                    cp = draft.get("curriculum")
                    if not cp or draft.get("selected_pages"):
                        raise ValueError("CURRICULUM_REVIEW_REQUIRED")
                    identities = [(s["name"], s["phase"]) for s in cp["subjects"]]
                    if len(identities) != len(set(identities)):
                        raise ValueError("CURRICULUM_SUBJECT_DUPLICATE")
                    count = 0
                    for subject in cp["subjects"]:
                        seen: set[str] = set()
                        for element in subject["elements"]:
                            verify_source(
                                element["description"],
                                element["page_start"],
                                element["page_end"],
                                pages,
                            )
                            for statement in element["statements"]:
                                verify_source(
                                    statement["description"],
                                    statement["page_start"],
                                    statement["page_end"],
                                    pages,
                                )
                                text = normalized(statement["description"])
                                if not (
                                    element["page_start"]
                                    <= statement["page_start"]
                                    <= statement["page_end"]
                                    <= element["page_end"]
                                ):
                                    raise ValueError("STATEMENT_OUTSIDE_ELEMENT")
                                if text not in normalized(element["description"]):
                                    raise ValueError("STATEMENT_OUTSIDE_ELEMENT")
                                if text in seen:
                                    raise ValueError("CURRICULUM_STATEMENT_DUPLICATE")
                                seen.add(text)
                                count += 1
                    if not 1 <= count <= self._policy.max_statements:
                        raise ValueError("CURRICULUM_STATEMENT_LIMIT")
                else:
                    selected = draft.get("selected_pages") or []
                    if draft.get("curriculum") or not selected or selected != sorted(set(selected)):
                        raise ValueError("GUIDANCE_REVIEW_REQUIRED")
                    for page in selected:
                        verify_source(pages.get(page, ""), page, page, pages)
            except ValueError as exc:
                raise InvalidInput(str(exc)) from exc
            updated = await self._uow.national_references.review(document_id, revision, draft)
            await self._uow.audit.record(
                None,
                actor_id,
                "platform.review_reference",
                "national_reference_documents",
                document_id,
                {"revision": updated},
            )
            return updated


class PublishReferenceHandler:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        actor_id: UUID,
        document_id: UUID,
        revision: int,
        key: UUID,
        *,
        retry: bool = False,
    ) -> ReferenceRow:
        async with self._uow:
            await require_platform(self._uow, actor_id)
            row = await self._uow.national_references.get(document_id, lock=True)
            if row is None:
                raise NotFound()
            request_id, _ = await self._uow.administration.request(
                actor_id,
                "reference.retry" if retry else "reference.publish",
                document_id,
                key,
                request_digest(str(revision)),
            )
            receipt = await self._uow.administration.request_receipt(request_id)
            if receipt:
                return receipt
            if row["revision"] != revision or row["status"] != ("failed" if retry else "review"):
                raise Conflict("REFERENCE_REVISION_CONFLICT")
            if not retry and row["review"] is None:
                raise Conflict("REFERENCE_REVIEW_REQUIRED")
            kind = INDEX_REFERENCE if row["review"] else EXTRACT_REFERENCE
            job_id = await queue_reference(self._uow, document_id, actor_id, kind)
            receipt = {
                "document_id": str(document_id),
                "job_id": str(job_id),
                "status": "indexing" if kind == INDEX_REFERENCE else "extracting",
            }
            await self._uow.administration.finish_receipt(request_id, document_id, receipt)
        return receipt
