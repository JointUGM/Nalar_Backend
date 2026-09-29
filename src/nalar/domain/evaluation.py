from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from nalar.domain.context_pack import RUBRIC_DIMENSIONS


@dataclass(frozen=True)
class ScoreCheck:
    dimension: str
    level: int
    quotes: tuple[tuple[UUID, str], ...]


@dataclass(frozen=True)
class OutcomeCheck:
    concept_id: UUID
    outcome: str
    misconception_id: UUID | None
    initial_misconception_id: UUID | None = None
    resolved_in_session: bool = False
    evidence_turn_id: UUID | None = None


def output_problems(
    answers: Mapping[UUID, str],
    scores: Sequence[ScoreCheck],
    outcomes: Sequence[OutcomeCheck],
    *,
    turn_ids: set[UUID],
    target_ids: set[UUID],
    misconception_concepts: Mapping[UUID, UUID],
) -> list[str]:
    """The backend's re-check of S4 output: literal quotes (AI-6 evidence), shape, and every
    reference the database would otherwise reject."""
    problems: list[str] = []
    if sorted(s.dimension for s in scores) != sorted(RUBRIC_DIMENSIONS):
        problems.append("scores must cover each rubric dimension exactly once")
    for score in scores:
        if not 0 <= score.level <= 4:
            problems.append(f"{score.dimension}: level {score.level} is out of range")
        if not score.quotes:
            problems.append(f"{score.dimension}: no evidence")
        for turn_id, quote in score.quotes:
            answer = answers.get(turn_id)
            if answer is None:
                problems.append(f"{score.dimension}: quote cites a turn outside the session")
            elif not quote.strip() or quote not in answer:
                problems.append(f"{score.dimension}: quote is not the student's own words")
    repeated = [c for c, n in Counter(o.concept_id for o in outcomes).items() if n > 1]
    if repeated:
        problems.append(f"more than one outcome for concepts {repeated}")
    for outcome in outcomes:
        if outcome.concept_id not in target_ids:
            problems.append(f"outcome for a concept that is not a target: {outcome.concept_id}")
        if (outcome.outcome == "misconception") != (outcome.misconception_id is not None):
            problems.append(f"outcome {outcome.outcome} and its misconception disagree")
        for m in (outcome.misconception_id, outcome.initial_misconception_id):
            if m is not None and misconception_concepts.get(m) != outcome.concept_id:
                problems.append(f"misconception {m} is not in the pack for {outcome.concept_id}")
        if outcome.resolved_in_session and outcome.initial_misconception_id is None:
            problems.append(f"{outcome.concept_id}: resolved without an initial misconception")
        if outcome.evidence_turn_id is not None and outcome.evidence_turn_id not in turn_ids:
            problems.append(f"{outcome.concept_id}: evidence turn is outside the session")
    return problems
