from dataclasses import dataclass
from uuid import UUID

from nalar.application.errors import Conflict, NotFound
from nalar.application.features.knowledge_base.commands.add_material import require_owner
from nalar.application.features.knowledge_base.commands.build_section import S1Settings
from nalar.application.features.knowledge_base.embeddings import embed_item
from nalar.application.ports.ai import AiGateway
from nalar.application.ports.knowledge import (
    ConceptView,
    ItemKind,
    ItemPatch,
    ItemRef,
    MisconceptionView,
)
from nalar.application.ports.uow import UnitOfWork


@dataclass(frozen=True)
class EditItem:
    actor_id: UUID
    kind: ItemKind
    item_id: UUID
    patch: ItemPatch


def _embed_text(ref: ItemRef, patch: ItemPatch) -> str | None:
    if ref.kind == "concept":
        if patch.name is None and patch.description is None:
            return None
        name = patch.name if patch.name is not None else ref.name
        description = patch.description if patch.description is not None else ref.description
        if (name, description) == (ref.name, ref.description):
            return None
        return f"{name.strip()}: {(description or '').strip()}"
    return patch.statement if patch.statement != ref.name else None


class EditItemHandler:
    def __init__(self, uow: UnitOfWork, ai: AiGateway, settings: S1Settings) -> None:
        self._uow = uow
        self._ai = ai
        self._model = settings.embedding_model

    async def execute(self, cmd: EditItem) -> ConceptView | MisconceptionView:
        async with self._uow:
            ref = await self._uow.knowledge.item_ref(cmd.kind, cmd.item_id)
            if ref is None:
                raise NotFound()
            await require_owner(self._uow, cmd.actor_id, ref.knowledge_base_id)
            if ref.review_status != "pending":
                raise Conflict(
                    "ITEM_NOT_PENDING", "Hanya butir yang belum ditinjau yang bisa diubah."
                )
        text = _embed_text(ref, cmd.patch)
        vector, invocations = (
            (None, [])
            if text is None
            else await embed_item(self._uow, self._ai, self._model, ref, text)
        )
        if invocations:
            # AI-6: paid calls survive a later authorization or state conflict.
            async with self._uow:
                await self._uow.ai_invocations.record(ref.school_id, invocations)
        async with self._uow:
            await require_owner(self._uow, cmd.actor_id, ref.knowledge_base_id)
            current = await self._uow.knowledge.item_ref(cmd.kind, cmd.item_id, lock=True)
            if current is None:
                raise NotFound()
            if current.review_status != "pending":
                raise Conflict("ITEM_NOT_PENDING")
            if (current.name, current.description) != (ref.name, ref.description):
                raise Conflict(
                    "ITEM_CHANGED", "Butir ini sudah diubah. Muat ulang sebelum menyimpan."
                )
            if not await self._uow.knowledge.edit_item(
                cmd.kind,
                cmd.item_id,
                cmd.patch,
                vector,
                self._model if vector is not None else None,
            ):
                raise Conflict("ITEM_NOT_PENDING")
            await self._uow.audit.record(
                ref.school_id,
                cmd.actor_id,
                "kb.item_edited",
                "concepts" if cmd.kind == "concept" else "misconceptions",
                ref.id,
                {k: v for k, v in vars(cmd.patch).items() if v is not None},
            )
            view = (
                await self._uow.knowledge.concept_view(ref.id)
                if cmd.kind == "concept"
                else await self._uow.knowledge.misconception_view(ref.id)
            )
        assert view is not None
        return view
