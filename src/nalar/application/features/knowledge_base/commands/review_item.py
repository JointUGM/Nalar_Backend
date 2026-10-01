from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from nalar.application.errors import Conflict, NotFound
from nalar.application.features.knowledge_base.commands.add_material import require_owner
from nalar.application.ports.clock import Clock
from nalar.application.ports.knowledge import ItemKind
from nalar.application.ports.uow import UnitOfWork


@dataclass(frozen=True)
class ReviewItem:
    actor_id: UUID
    kind: ItemKind
    item_id: UUID
    status: Literal["approved", "rejected"]


@dataclass(frozen=True)
class ItemReviewed:
    id: UUID
    review_status: str
    reviewed_at: datetime


class ReviewItemHandler:
    def __init__(self, uow: UnitOfWork, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock

    async def execute(self, cmd: ReviewItem) -> ItemReviewed:
        async with self._uow:
            ref = await self._uow.knowledge.item_ref(cmd.kind, cmd.item_id)
            if ref is None:
                raise NotFound()
            await require_owner(self._uow, cmd.actor_id, ref.knowledge_base_id)
            ref = await self._uow.knowledge.item_ref(cmd.kind, cmd.item_id, lock=True)
            if ref is None:
                raise NotFound()
            if ref.review_status != "pending":
                if ref.review_status != cmd.status or ref.reviewed_at is None:
                    raise Conflict("ITEM_NOT_PENDING", "Butir ini sudah ditinjau.")
                return ItemReviewed(ref.id, ref.review_status, ref.reviewed_at)
            now = self._clock.now()
            outcome = await self._uow.knowledge.review_item(
                cmd.kind, cmd.item_id, cmd.status, cmd.actor_id, now
            )
            if outcome == "concept_not_approved":
                raise Conflict("CONCEPT_NOT_APPROVED", "Setujui konsepnya terlebih dahulu.")
            if outcome == "reviewed":
                await self._uow.audit.record(
                    ref.school_id,
                    cmd.actor_id,
                    "kb.item_reviewed",
                    "concepts" if cmd.kind == "concept" else "misconceptions",
                    ref.id,
                    {"review_status": cmd.status},
                )
                return ItemReviewed(ref.id, cmd.status, now)
            raise Conflict("ITEM_NOT_PENDING", "Butir ini sudah ditinjau.")
