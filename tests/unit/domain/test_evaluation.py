from uuid import UUID, uuid4

from nalar.domain.evaluation import OutcomeCheck, ScoreCheck, output_problems

T1, T2, M, TURN, UNANSWERED = uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
ANSWERS = {TURN: "Kelereng berhenti karena gaya gesek dengan lantai."}


def scores(quote: str = "karena gaya gesek", turn: UUID = TURN, level: int = 2) -> list[ScoreCheck]:
    return [
        ScoreCheck(d, level, ((turn, quote),))
        for d in ("claim", "evidence", "mechanism", "transfer")
    ]


def outcomes() -> list[OutcomeCheck]:
    return [OutcomeCheck(T1, "mastered", None), OutcomeCheck(T2, "misconception", M)]


def problems(score_checks: list[ScoreCheck], outcome_checks: list[OutcomeCheck]) -> list[str]:
    return output_problems(
        ANSWERS,
        score_checks,
        outcome_checks,
        turn_ids={TURN, UNANSWERED},
        target_ids={T1, T2},
        misconception_ids={M},
    )


def test_faithful_output_has_no_problems() -> None:
    assert problems(scores(), outcomes()) == []


def test_a_fabricated_quote_is_a_problem() -> None:
    assert problems(scores("karena gayanya habis"), outcomes())


def test_a_quote_from_another_sessions_turn_is_a_problem() -> None:
    assert problems(scores(turn=uuid4()), outcomes())


def test_every_dimension_once_with_a_level_in_range_and_evidence() -> None:
    assert problems(scores()[:3], outcomes())
    assert problems(scores(level=5), outcomes())
    assert problems([ScoreCheck("claim", 1, ()), *scores()[1:]], outcomes())


def test_outcomes_must_be_targets_and_consistent() -> None:
    assert problems(scores(), [OutcomeCheck(uuid4(), "mastered", None)])
    assert problems(scores(), [OutcomeCheck(T1, "misconception", None)])
    assert problems(scores(), [OutcomeCheck(T1, "mastered", None)] * 2)


def test_outcome_references_must_exist_in_this_session_and_pack() -> None:
    assert problems(scores(), [OutcomeCheck(T2, "misconception", uuid4())])
    assert problems(scores(), [OutcomeCheck(T1, "mastered", None, evidence_turn_id=uuid4())])
    assert problems(scores(), [OutcomeCheck(T1, "mastered", None, resolved_in_session=True)])
    assert not problems(
        scores(),
        [OutcomeCheck(T1, "mastered", None, M, resolved_in_session=True, evidence_turn_id=TURN)],
    )
