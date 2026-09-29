from collections.abc import Callable
from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from nalar.application.ports.ai_contract import ContextPackIn
from nalar.domain.context_pack import (
    BankQuestion,
    PackMisconception,
    Target,
    VersionContent,
    build_context_pack,
    derive_probe_plan,
    question_bank_from_pack,
    validate_version,
)
from nalar.domain.labels import PROBE_MOVES

T1, T2 = uuid4(), uuid4()
M1 = uuid4()


def bank(targets: tuple[UUID, ...] = (T1, T2)) -> tuple[BankQuestion, ...]:
    return tuple(
        BankQuestion(id=f"{i}-{move}", concept_id=t, move=move, text=f"Pertanyaan {move}?")
        for i, t in enumerate(targets)
        for move in PROBE_MOVES
    )


def valid() -> VersionContent:
    return VersionContent(
        anchor_problem="Kelereng didorong lalu berhenti. Kenapa?",
        reference_reasoning="Gaya gesek memperlambat kelereng sampai berhenti.",
        rubric={
            d: [f"{d} {n}" for n in range(5)]
            for d in ("claim", "evidence", "mechanism", "transfer")
        },
        targets=(Target(T1, "Gaya gesek", ""), Target(T2, "Hukum I Newton", "")),
        misconceptions=(PackMisconception(M1, T1, "Gaya bisa habis", ("habis",)),),
        question_bank=bank(),
        answer_terms=("gesek", "gaya"),
        max_turns=6,
        max_duration_minutes=20,
    )


def test_a_valid_version_has_no_problems() -> None:
    assert validate_version(valid()) == []


MUTATIONS: dict[str, Callable[[VersionContent], VersionContent]] = {
    "TARGET_COUNT": lambda v: replace(v, targets=v.targets[:1], question_bank=bank((T1,))),
    "TARGET_DUPLICATE": lambda v: replace(v, targets=(v.targets[0], v.targets[0])),
    "MAX_TURNS_RANGE": lambda v: replace(v, max_turns=11),
    "MAX_TURNS_BELOW_TARGETS": lambda v: replace(
        v, targets=(*v.targets, Target(uuid4(), "Gaya normal", "")), max_turns=2
    ),
    "DURATION_RANGE": lambda v: replace(v, max_duration_minutes=61),
    "RUBRIC_SHAPE": lambda v: replace(v, rubric={**v.rubric, "claim": ["a", "b"]}),
    "ANSWER_TERMS_RANGE": lambda v: replace(v, answer_terms=()),
    "QUESTION_ID_DUPLICATE": lambda v: replace(
        v, question_bank=(*v.question_bank, v.question_bank[0])
    ),
    "QUESTION_NOT_TARGET": lambda v: replace(
        v,
        question_bank=(
            *v.question_bank,
            BankQuestion("x", uuid4(), "transfer", "Pertanyaan lain?"),
        ),
    ),
    "QUESTION_MISCONCEPTION_MISMATCH": lambda v: replace(
        v,
        question_bank=(
            *v.question_bank,
            BankQuestion("y", T2, "transfer", "Pertanyaan lain?", misconception_id=M1),
        ),
    ),
    "MISCONCEPTION_NOT_TARGET": lambda v: replace(
        v, misconceptions=(PackMisconception(uuid4(), uuid4(), "Salah paham", ()),)
    ),
    "MOVE_MISSING": lambda v: replace(
        v, question_bank=tuple(q for q in v.question_bank if q.move != "transfer")
    ),
    "UNKNOWN_MOVE": lambda v: replace(
        v,
        question_bank=(*v.question_bank, BankQuestion("z", T1, "vary_variable", "Ubah?")),
    ),
}


@pytest.mark.parametrize("code", sorted(MUTATIONS))
def test_each_problem_is_reported_on_its_own(code: str) -> None:
    problems = validate_version(MUTATIONS[code](valid()))
    assert code in {p.code for p in problems}


def test_built_pack_validates_against_the_ai_contract() -> None:
    parsed = ContextPackIn.model_validate(build_context_pack(valid()))
    assert parsed.max_probes == 6
    assert len(parsed.question_bank) == 16
    assert {t.id for t in parsed.targets} == {T1, T2}


def test_bank_round_trips_through_the_stored_pack() -> None:
    assert question_bank_from_pack(build_context_pack(valid())) == valid().question_bank


def test_probe_plan_groups_question_ids_by_move() -> None:
    plan = derive_probe_plan(valid().question_bank)
    assert set(plan) == set(PROBE_MOVES)
    assert plan["transfer"] == ["0-transfer", "1-transfer"]
