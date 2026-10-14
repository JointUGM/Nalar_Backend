from uuid import UUID, uuid4

from nalar.domain.class_map import ConceptResult, StudentAttempt
from nalar.domain.misconception_trail import trail

C1, M1, M2, OTHER = uuid4(), uuid4(), uuid4(), uuid4()


def held(student: UUID, misconception: UUID = M1) -> StudentAttempt:
    result = ConceptResult(C1, "misconception", misconception, misconception, False)
    return StudentAttempt(student, "completed", "completed", (result,))


def later(
    student: UUID,
    outcome: str,
    misconception: UUID | None = None,
    status: str = "completed",
    evaluation: str | None = "completed",
) -> StudentAttempt:
    result = ConceptResult(C1, outcome, misconception, None, False)
    return StudentAttempt(student, status, evaluation, (result,))


def test_every_holder_lands_in_exactly_one_bucket() -> None:
    s = [uuid4() for _ in range(8)]
    earlier = [held(i) for i in s]
    after = [
        later(s[0], "mastered"),
        later(s[1], "developing"),
        later(s[2], "misconception", OTHER),
        later(s[3], "misconception", M1),
        later(s[4], "not_observed"),
        later(s[5], "mastered", status="timed_out"),
        later(s[6], "mastered", evaluation=None),
    ]
    result = trail([(M1, C1)], {C1}, {M1}, earlier, after)
    [item] = result.items
    assert item.held_count == 8
    assert (
        item.resolved_breakdown.mastered,
        item.resolved_breakdown.developing,
        item.resolved_breakdown.other_misconception,
    ) == (1, 1, 1)
    assert item.still_held_ids == (s[3],)
    assert item.not_observed_count == 4
    assert (
        item.held_count == item.resolved_count + len(item.still_held_ids) + item.not_observed_count
    )


def test_a_misconception_missing_from_the_later_pack_is_never_resolved() -> None:
    s = uuid4()
    result = trail(
        [(M1, C1), (M2, C1)], {C1}, {M2}, [held(s, M1), held(s, M2)], [later(s, "mastered")]
    )
    assert [(n.misconception_id, n.held_count) for n in result.not_retested] == [(M1, 1)]
    assert [i.misconception_id for i in result.items] == [M2]


def test_a_concept_the_later_mission_does_not_target_is_not_retested() -> None:
    s = uuid4()
    result = trail([(M1, C1)], set(), {M1}, [held(s)], [later(s, "mastered")])
    assert (result.items, [n.concept_id for n in result.not_retested]) == ((), [C1])


def test_students_who_did_not_hold_it_or_have_no_eligible_attempt_are_not_counted() -> None:
    fixed, unfinished, mastered = uuid4(), uuid4(), uuid4()
    earlier = [
        StudentAttempt(
            fixed, "completed", "completed", (ConceptResult(C1, "mastered", None, M1, True),)
        ),
        StudentAttempt(
            unfinished, "completed", None, (ConceptResult(C1, "misconception", M1, M1, False),)
        ),
        later(mastered, "mastered"),
    ]
    assert trail([(M1, C1)], {C1}, {M1}, earlier, []) == trail([], set(), set(), [], [])
