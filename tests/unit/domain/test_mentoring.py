from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from nalar.domain.context_pack import BankQuestion, PackMisconception
from nalar.domain.mentoring import MentoringContext, MentoringTurn, help_signal

NOW = datetime(2026, 10, 14, tzinfo=UTC)
M = UUID(int=1)
C = UUID(int=2)


def context() -> MentoringContext:
    return MentoringContext(
        UUID(int=3),
        NOW + timedelta(minutes=10),
        (
            MentoringTurn(2, NOW, "misconception", M, False, "counter_example", "q", C),
            MentoringTurn(1, NOW, "misconception", M, False, "request_justification", None, C),
        ),
        (PackMisconception(M, C, "Gaya habis", ()),),
        (BankQuestion("q", C, "counter_example", "Bagaimana jika permukaannya berbeda?", M),),
    )


def test_frustration_takes_precedence_over_persistence() -> None:
    data = context()
    data = replace(data, turns=(replace(data.turns[0], frustration=True), data.turns[1]))
    found = help_signal(data, NOW)
    assert found and found.reason == "frustration"


def test_two_adjacent_evasive_answers_need_support() -> None:
    data = context()
    data = replace(data, turns=tuple(replace(t, state="evasive") for t in data.turns))
    found = help_signal(data, NOW)
    assert found and found.reason == "repeated_evasive" and found.misconception_statement is None


def test_answer_to_matching_counter_example_preserves_misconception() -> None:
    found = help_signal(context(), NOW)
    assert found and found.reason == "persistent_misconception"
    assert found.suggestion == "Coba tanyakan: Bagaimana jika permukaannya berbeda?"
    assert found.turn_index == 2


@pytest.mark.parametrize(
    "change",
    [
        {"state": None},
        {"state": "correct_reasoned"},
        {"move": "request_justification"},
        {"question_bank_id": "missing"},
        {"misconception_id": UUID(int=4)},
        {"target_concept_id": UUID(int=4)},
    ],
)
def test_uncertain_recovered_or_wrong_target_answer_has_no_signal(
    change: dict[str, Any],
) -> None:
    data = context()
    data = replace(data, turns=(replace(data.turns[0], **change), data.turns[1]))
    assert help_signal(data, NOW) is None


def test_unmapped_counter_example_cannot_prove_persistence() -> None:
    data = context()
    data = replace(data, questions=(replace(data.questions[0], misconception_id=None),))
    assert help_signal(data, NOW) is None


def test_expired_session_has_no_signal() -> None:
    assert help_signal(replace(context(), deadline_at=NOW), NOW) is None
