from uuid import uuid4

from nalar.domain.class_map import ConceptResult, StudentAttempt, aggregate

C1, C2, M1 = uuid4(), uuid4(), uuid4()


def attempt(
    status: str = "completed", evaluation: str | None = "completed", *results: ConceptResult
) -> StudentAttempt:
    return StudentAttempt(uuid4(), status, evaluation, tuple(results))


def test_only_completed_and_evaluated_latest_attempts_are_counted() -> None:
    mastered = ConceptResult(C1, "mastered", None, None, False)
    wrong = ConceptResult(C1, "misconception", M1, M1, False)
    fixed = ConceptResult(C1, "developing", None, M1, True)
    result = aggregate(
        [C1, C2],
        {C1: [M1]},
        [
            attempt("completed", "completed", mastered),
            attempt("completed", "completed", wrong),
            attempt("completed", "completed", fixed),
            attempt("timed_out", "completed", mastered),
            attempt("completed", None),
            attempt("completed", "failed", mastered),
            attempt("in_progress", None),
        ],
    )
    assert (result.denominator, result.incomplete_count) == (3, 4)
    c1, c2 = result.concepts
    assert (c1.mastered_count, c1.developing_count, c1.not_observed_count) == (1, 1, 0)
    [m] = c1.misconceptions
    assert (m.count, m.resolved_count, len(m.student_ids)) == (1, 1, 1)
    assert c2.not_observed_count == 3
