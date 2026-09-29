from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class ConceptResult:
    concept_id: UUID
    outcome: str
    misconception_id: UUID | None
    initial_misconception_id: UUID | None
    resolved: bool


@dataclass(frozen=True)
class StudentAttempt:
    """A student's latest attempt: an older attempt is never reused (TC-9)."""

    student_id: UUID
    status: str
    evaluation_status: str | None
    results: tuple[ConceptResult, ...]


@dataclass(frozen=True)
class MisconceptionCount:
    misconception_id: UUID
    count: int
    resolved_count: int
    student_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class ConceptCount:
    concept_id: UUID
    mastered_count: int
    developing_count: int
    not_observed_count: int
    misconceptions: tuple[MisconceptionCount, ...]


@dataclass(frozen=True)
class ClassMap:
    denominator: int
    incomplete_count: int
    concepts: tuple[ConceptCount, ...]


def aggregate(
    concept_ids: Sequence[UUID],
    misconceptions_by_concept: Mapping[UUID, Sequence[UUID]],
    attempts: Sequence[StudentAttempt],
) -> ClassMap:
    """AI-4: exact counts over eligible latest attempts; no model is involved."""
    eligible = [
        a for a in attempts if a.status == "completed" and a.evaluation_status == "completed"
    ]
    concepts = []
    for concept_id in concept_ids:
        results = {
            a.student_id: next((r for r in a.results if r.concept_id == concept_id), None)
            for a in eligible
        }
        outcomes = [r.outcome if r else "not_observed" for r in results.values()]
        misconceptions = []
        for misconception_id in misconceptions_by_concept.get(concept_id, ()):
            holders = tuple(
                s
                for s, r in results.items()
                if r and r.outcome == "misconception" and r.misconception_id == misconception_id
            )
            resolved = sum(
                1
                for r in results.values()
                if r and r.resolved and r.initial_misconception_id == misconception_id
            )
            misconceptions.append(
                MisconceptionCount(misconception_id, len(holders), resolved, holders)
            )
        concepts.append(
            ConceptCount(
                concept_id=concept_id,
                mastered_count=outcomes.count("mastered"),
                developing_count=outcomes.count("developing"),
                not_observed_count=outcomes.count("not_observed"),
                misconceptions=tuple(misconceptions),
            )
        )
    return ClassMap(len(eligible), len(attempts) - len(eligible), tuple(concepts))
