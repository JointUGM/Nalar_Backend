from collections.abc import Collection, Sequence
from dataclasses import dataclass
from uuid import UUID

from nalar.domain.class_map import ConceptResult, StudentAttempt


@dataclass(frozen=True)
class ResolvedBreakdown:
    mastered: int
    developing: int
    other_misconception: int


@dataclass(frozen=True)
class TrailItem:
    concept_id: UUID
    misconception_id: UUID
    held_count: int
    resolved_breakdown: ResolvedBreakdown
    still_held_ids: tuple[UUID, ...]
    not_observed_count: int

    @property
    def resolved_count(self) -> int:
        b = self.resolved_breakdown
        return b.mastered + b.developing + b.other_misconception


@dataclass(frozen=True)
class NotRetested:
    concept_id: UUID
    misconception_id: UUID
    held_count: int


@dataclass(frozen=True)
class Trail:
    items: tuple[TrailItem, ...]
    not_retested: tuple[NotRetested, ...]


def _result(attempt: StudentAttempt | None, concept_id: UUID) -> ConceptResult | None:
    if attempt is None or not attempt.eligible:
        return None
    return next((r for r in attempt.results if r.concept_id == concept_id), None)


def trail(
    earlier_misconceptions: Sequence[tuple[UUID, UUID]],
    later_concept_ids: Collection[UUID],
    later_misconception_ids: Collection[UUID],
    earlier: Sequence[StudentAttempt],
    later: Sequence[StudentAttempt],
) -> Trail:
    """AI-4: exact counts, no model. A misconception the later pack cannot report (F3) is
    never called resolved."""
    later_by_student = {a.student_id: a for a in later}
    items: list[TrailItem] = []
    not_retested: list[NotRetested] = []
    for misconception_id, concept_id in earlier_misconceptions:
        holders = [
            a.student_id
            for a in earlier
            if (r := _result(a, concept_id))
            and r.outcome == "misconception"
            and r.misconception_id == misconception_id
        ]
        if not holders:
            continue
        if concept_id not in later_concept_ids or misconception_id not in later_misconception_ids:
            not_retested.append(NotRetested(concept_id, misconception_id, len(holders)))
            continue
        outcomes: dict[str, int] = {"mastered": 0, "developing": 0, "other": 0, "not_observed": 0}
        still_held: list[UUID] = []
        for student_id in holders:
            r = _result(later_by_student.get(student_id), concept_id)
            if r is None or r.outcome == "not_observed":
                outcomes["not_observed"] += 1
            elif r.outcome == "misconception" and r.misconception_id == misconception_id:
                still_held.append(student_id)
            elif r.outcome == "misconception":
                outcomes["other"] += 1
            else:
                outcomes[r.outcome] += 1
        items.append(
            TrailItem(
                concept_id,
                misconception_id,
                len(holders),
                ResolvedBreakdown(outcomes["mastered"], outcomes["developing"], outcomes["other"]),
                tuple(still_held),
                outcomes["not_observed"],
            )
        )
    return Trail(tuple(items), tuple(not_retested))
