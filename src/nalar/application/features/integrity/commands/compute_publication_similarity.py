from collections import defaultdict
from uuid import UUID

from nalar.application.ports.uow import UnitOfWork
from nalar.domain.integrity import FlagDraft, IntegrityConfig, similarity_flags


class ComputePublicationSimilarityHandler:
    """Cross-student similarity, run by the publication finalizer once every run is closed."""

    def __init__(self, uow: UnitOfWork, config: IntegrityConfig) -> None:
        self._uow = uow
        self._config = config

    async def execute(self, publication_id: UUID) -> int:
        async with self._uow:
            school_id = await self._uow.integrity.school_of_publication(publication_id)
            if school_id is None:
                return 0
            answers = await self._uow.integrity.publication_answers(publication_id)
            by_session: defaultdict[UUID, list[FlagDraft]] = defaultdict(list)
            for session_id, draft in similarity_flags(answers, self._config):
                by_session[session_id].append(draft)
            added = 0
            for session_id, drafts in by_session.items():
                added += await self._uow.integrity.insert_flags(school_id, session_id, drafts)
            return added
