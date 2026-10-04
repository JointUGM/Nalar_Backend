from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from nalar.application.errors import NotFound
from nalar.application.ports.uow import UnitOfWork
from nalar.domain.sessions import TERMINAL_STATUSES


@dataclass(frozen=True)
class OpeningGuess:
    choice_id: str
    text: str


@dataclass(frozen=True)
class Reflection:
    mission_title: str
    completed_at: datetime | None
    content: str
    opening_guess: OpeningGuess | None
    subject_name: str = ""


class ReflectionQuery:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, actor_id: UUID, session_id: UUID) -> Reflection | None:
        """None while the evaluation is still pending."""
        async with self._uow:
            if not await self._uow.authz.owns_session(actor_id, session_id):
                raise NotFound()
            view = await self._uow.evaluations.reflection_view(session_id)
        if view is None or view.status not in TERMINAL_STATUSES:
            raise NotFound()
        if not view.evaluated:
            return None
        if view.content is None:
            raise NotFound()
        guess = None
        if view.warmup_choice_id and view.live_warmup:
            text = next(
                (
                    str(c["text"])
                    for c in view.live_warmup.get("choices", [])
                    if c.get("id") == view.warmup_choice_id
                ),
                None,
            )
            guess = OpeningGuess(view.warmup_choice_id, text) if text else None
        return Reflection(view.mission_title, view.ended_at, view.content, guess, view.subject_name)
