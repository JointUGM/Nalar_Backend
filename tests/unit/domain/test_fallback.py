from uuid import uuid4

import pytest

from nalar.domain.context_pack import BankQuestion
from nalar.domain.fallback import fallback_question

T1, T2 = uuid4(), uuid4()
BANK = (
    BankQuestion("a", T1, "transfer", "Transfer?"),
    BankQuestion("b", T1, "request_justification", "Kenapa begitu? (T1)"),
    BankQuestion("c", T2, "request_justification", "Kenapa begitu? (T2)"),
)


def test_fallback_uses_the_last_targets_justification_question() -> None:
    assert fallback_question(BANK, T2).id == "c"


def test_fallback_without_a_matching_target_uses_the_first_justification() -> None:
    assert fallback_question(BANK, uuid4()).id == "b"
    assert fallback_question(BANK, None).id == "b"


def test_fallback_requires_a_justification_question() -> None:
    with pytest.raises(ValueError):
        fallback_question(BANK[:1], T1)
