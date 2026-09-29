from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

from nalar.domain.labels import PROBE_MOVES

RUBRIC_DIMENSIONS: Final = ("claim", "evidence", "mechanism", "transfer")
RUBRIC_LEVELS: Final = 5


@dataclass(frozen=True)
class Target:
    id: UUID
    name: str
    description: str


@dataclass(frozen=True)
class PackMisconception:
    id: UUID
    concept_id: UUID
    statement: str
    detection_cues: tuple[str, ...]


@dataclass(frozen=True)
class BankQuestion:
    id: str
    concept_id: UUID
    move: str
    text: str
    misconception_id: UUID | None = None


@dataclass(frozen=True)
class VersionContent:
    anchor_problem: str
    reference_reasoning: str
    rubric: Mapping[str, Sequence[str]]
    targets: tuple[Target, ...]
    misconceptions: tuple[PackMisconception, ...]
    question_bank: tuple[BankQuestion, ...]
    answer_terms: tuple[str, ...]
    max_turns: int
    max_duration_minutes: int


@dataclass(frozen=True)
class Problem:
    code: str
    detail: str


def validate_version(v: VersionContent) -> list[Problem]:
    """Mirrors the AI's validate_pack plus the contract's limits (D-S10-1)."""
    problems: list[Problem] = []
    target_ids = [t.id for t in v.targets]
    targets = set(target_ids)
    if not 2 <= len(target_ids) <= 3:
        problems.append(Problem("TARGET_COUNT", "Pilih 2 sampai 3 konsep target."))
    if len(targets) != len(target_ids):
        problems.append(Problem("TARGET_DUPLICATE", "Konsep target tidak boleh berulang."))
    if not 2 <= v.max_turns <= 10:
        problems.append(Problem("MAX_TURNS_RANGE", "Jumlah giliran harus 2 sampai 10."))
    if v.max_turns < len(target_ids):
        problems.append(
            Problem("MAX_TURNS_BELOW_TARGETS", "Jumlah giliran kurang dari jumlah target.")
        )
    if not 5 <= v.max_duration_minutes <= 60:
        problems.append(Problem("DURATION_RANGE", "Durasi harus 5 sampai 60 menit."))
    if set(v.rubric) != set(RUBRIC_DIMENSIONS) or any(
        len(levels) != RUBRIC_LEVELS or not all(str(d).strip() for d in levels)
        for levels in v.rubric.values()
    ):
        problems.append(
            Problem("RUBRIC_SHAPE", "Rubrik harus punya 4 dimensi, masing-masing 5 deskriptor.")
        )
    if not 1 <= len(v.answer_terms) <= 50:
        problems.append(Problem("ANSWER_TERMS_RANGE", "Istilah jawaban harus 1 sampai 50."))

    concept_of = {m.id: m.concept_id for m in v.misconceptions}
    for m in v.misconceptions:
        if m.concept_id not in targets:
            problems.append(Problem("MISCONCEPTION_NOT_TARGET", f"Miskonsepsi {m.id}"))

    seen: set[str] = set()
    for q in v.question_bank:
        if q.id in seen:
            problems.append(Problem("QUESTION_ID_DUPLICATE", q.id))
        seen.add(q.id)
        if q.move not in PROBE_MOVES:
            problems.append(Problem("UNKNOWN_MOVE", q.id))
        if q.concept_id not in targets:
            problems.append(Problem("QUESTION_NOT_TARGET", q.id))
        if q.misconception_id is not None and concept_of.get(q.misconception_id) != q.concept_id:
            problems.append(Problem("QUESTION_MISCONCEPTION_MISMATCH", q.id))

    covered = {(q.concept_id, q.move) for q in v.question_bank}
    for target in v.targets:
        missing = [move for move in PROBE_MOVES if (target.id, move) not in covered]
        if missing:
            problems.append(Problem("MOVE_MISSING", f"{target.name}: {', '.join(missing)}"))
    return problems


def build_context_pack(v: VersionContent) -> dict[str, Any]:
    return {
        "pack_version": 1,
        "anchor_problem": v.anchor_problem,
        "reference_reasoning": v.reference_reasoning,
        "max_probes": v.max_turns,
        "max_duration_minutes": v.max_duration_minutes,
        "targets": [
            {"id": str(t.id), "name": t.name, "description": t.description} for t in v.targets
        ],
        "misconceptions": [
            {
                "id": str(m.id),
                "concept_id": str(m.concept_id),
                "statement": m.statement,
                "detection_cues": list(m.detection_cues),
            }
            for m in v.misconceptions
        ],
        "question_bank": [_question_json(q) for q in v.question_bank],
        "answer_terms": list(v.answer_terms),
    }


def derive_probe_plan(bank: Iterable[BankQuestion]) -> dict[str, list[str]]:
    plan: dict[str, list[str]] = {move: [] for move in PROBE_MOVES}
    for q in bank:
        plan.setdefault(q.move, []).append(q.id)
    return plan


def question_bank_from_pack(pack: Mapping[str, Any]) -> tuple[BankQuestion, ...]:
    return tuple(
        BankQuestion(
            id=str(q["id"]),
            concept_id=UUID(str(q["concept_id"])),
            move=str(q["move"]),
            text=str(q["text"]),
            misconception_id=UUID(str(q["misconception_id"]))
            if q.get("misconception_id")
            else None,
        )
        for q in pack["question_bank"]
    )


def target_ids_from_pack(pack: Mapping[str, Any]) -> tuple[UUID, ...]:
    return tuple(UUID(str(t["id"])) for t in pack["targets"])


def _question_json(q: BankQuestion) -> dict[str, Any]:
    return {
        "id": q.id,
        "concept_id": str(q.concept_id),
        "misconception_id": str(q.misconception_id) if q.misconception_id else None,
        "move": q.move,
        "text": q.text,
    }
