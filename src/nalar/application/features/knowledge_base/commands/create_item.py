import hashlib
import json
from dataclasses import asdict
from uuid import UUID, uuid4

from nalar.application.errors import NotFound
from nalar.application.features.knowledge_base.commands.add_material import require_owner
from nalar.application.features.knowledge_base.commands.build_section import S1Settings
from nalar.application.features.knowledge_base.embeddings import embed_item
from nalar.application.ports.ai import AiGateway
from nalar.application.ports.knowledge import (
    ConceptView,
    ItemKind,
    ItemRef,
    ManualItem,
    MisconceptionView,
)
from nalar.application.ports.uow import UnitOfWork


class CreateItemHandler:
    def __init__(self, uow: UnitOfWork, ai: AiGateway, settings: S1Settings) -> None:
        self._uow, self._ai, self._model = uow, ai, settings.embedding_model

    async def execute(
        self, actor_id: UUID, kb_id: UUID, item: ManualItem, key: UUID
    ) -> ConceptView | MisconceptionView:
        kind: ItemKind = "misconception" if item.concept_id else "concept"
        digest = hashlib.sha256(
            json.dumps(asdict(item), sort_keys=True, default=str).encode()
        ).hexdigest()
        async with self._uow:
            await require_owner(self._uow, actor_id, kb_id)
            kb = await self._uow.knowledge.kb_ref(kb_id)
            assert kb is not None
            _, previous = await self._uow.administration.request(actor_id, kind, kb_id, key, digest)
            if previous:
                return await self._view(previous, kind)
            await self._uow.knowledge.validate_manual(kb_id, item)
        item_id = uuid4()
        ref = ItemRef(
            item_id, kind, kb_id, kb.school_id, "pending", None, item.name, item.description
        )
        text = item.name if item.concept_id else f"{item.name}: {item.description or ''}"
        vector, invocations = await embed_item(self._uow, self._ai, self._model, ref, text)
        async with self._uow:
            await self._uow.ai_invocations.record(kb.school_id, invocations)
        async with self._uow:
            await require_owner(self._uow, actor_id, kb_id)
            request_id, previous = await self._uow.administration.request(
                actor_id, kind, kb_id, key, digest
            )
            if previous:
                return await self._view(previous, kind)
            await self._uow.knowledge.validate_manual(kb_id, item)
            await self._uow.knowledge.create_manual(kb, item_id, item, vector, self._model)
            await self._uow.administration.finish_request(request_id, item_id)
            await self._uow.audit.record(
                kb.school_id,
                actor_id,
                "kb.item_created",
                "misconceptions" if item.concept_id else "concepts",
                item_id,
                {"origin": "teacher"},
            )
            return await self._view(item_id, kind)

    async def _view(self, item_id: UUID, kind: str) -> ConceptView | MisconceptionView:
        view = (
            await self._uow.knowledge.concept_view(item_id)
            if kind == "concept"
            else await self._uow.knowledge.misconception_view(item_id)
        )
        if view is None:
            raise NotFound()
        return view
