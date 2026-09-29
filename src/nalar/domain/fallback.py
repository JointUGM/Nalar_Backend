from collections.abc import Sequence
from uuid import UUID

from nalar.domain.context_pack import BankQuestion


def fallback_question(bank: Sequence[BankQuestion], last_target_id: UUID | None) -> BankQuestion:
    """NFR-R4: the approved justification question for the last target, else the first one."""
    justifications = [q for q in bank if q.move == "request_justification"]
    if not justifications:
        raise ValueError("the pack has no request_justification question")
    return next((q for q in justifications if q.concept_id == last_target_id), justifications[0])
