from uuid import UUID, uuid4

import pytest

from nalar.domain.integrity import AnswerFacts, TurnFacts, session_flags, similarity_flags
from nalar.domain.telemetry import TurnMetrics
from nalar.infrastructure.config import load_integrity_config

CFG = load_integrity_config()


def turn(
    index: int,
    answer: str = "x" * 100,
    quality: int | None = 2,
    paused: bool = False,
    **metrics: int,
) -> TurnFacts:
    return TurnFacts(uuid4(), index, answer, quality, paused, TurnMetrics(**metrics))


@pytest.mark.parametrize(
    ("pasted", "answer_len", "flagged"),
    [(60, 150, True), (59, 100, False), (61, 100, True), (60, 151, False), (80, 200, True)],
)
def test_large_paste_threshold(pasted: int, answer_len: int, flagged: bool) -> None:
    flags = session_flags([turn(0, "x" * answer_len, chars_pasted=pasted)], CFG)
    assert (["large_paste"] if flagged else []) == [f.flag_type for f in flags]
    if flagged:
        assert flags[0].evidence["config_version"] == CFG.version


@pytest.mark.parametrize(
    ("hidden_ms", "events", "flagged"),
    [(20000, 1, True), (19999, 1, False), (20001, 1, True), (100, 3, True), (100, 2, False)],
)
def test_tab_switching_is_one_session_level_flag(
    hidden_ms: int, events: int, flagged: bool
) -> None:
    turns = [turn(0, tab_hidden_ms=hidden_ms, tab_hidden_events=events)]
    flags = [f for f in session_flags(turns, CFG) if f.flag_type == "tab_switching"]
    assert len(flags) == (1 if flagged else 0)
    assert all(f.turn_id is None for f in flags)


@pytest.mark.parametrize(
    ("qualities", "flagged"),
    [((3, 1, 1), True), ((2, 1, 1), False), ((4, 0, 1), True), ((3, 2, 1), False), ((3, 1), False)],
)
def test_inconsistency_gap(qualities: tuple[int, ...], flagged: bool) -> None:
    turns = [turn(i, quality=q) for i, q in enumerate(qualities)]
    flags = [f for f in session_flags(turns, CFG) if f.flag_type == "inconsistency_gap"]
    assert len(flags) == (1 if flagged else 0)
    if flagged:
        assert flags[0].turn_id == turns[0].turn_id


@pytest.mark.parametrize(("jump", "flagged"), [(2, True), (1, False), (3, True)])
def test_disconnect_pattern(jump: int, flagged: bool) -> None:
    turns = [turn(0, quality=1), turn(1, quality=1 + jump, disconnect_events=1)]
    flags = [f for f in session_flags(turns, CFG) if f.flag_type == "disconnect_pattern"]
    assert len(flags) == (1 if flagged else 0)


def test_quality_jump_without_disconnect_is_not_flagged() -> None:
    turns = [turn(0, quality=0), turn(1, quality=4)]
    assert not [f for f in session_flags(turns, CFG) if f.flag_type == "disconnect_pattern"]


def test_safety_paused_turns_never_flag() -> None:
    turns = [
        turn(0, "x" * 100, quality=3, paused=True, chars_pasted=90, tab_hidden_ms=60000),
        turn(1, quality=1, paused=True, disconnect_events=1),
        turn(2, quality=1, paused=True),
    ]
    assert session_flags(turns, CFG) == []


def test_style_shift_is_never_emitted() -> None:
    turns = [
        turn(i, quality=q, chars_pasted=90, tab_hidden_ms=30000) for i, q in enumerate((3, 1, 1))
    ]
    assert "style_shift" not in {f.flag_type for f in session_flags(turns, CFG)}


def answer(session: UUID, index: int, text: str) -> AnswerFacts:
    return AnswerFacts(session, uuid4(), index, text)


def test_similar_answers_on_the_same_turn_flag_both_sessions() -> None:
    a, b = uuid4(), uuid4()
    text = "Kelereng berhenti karena gaya gesek antara kelereng dan lantai"
    flags = similarity_flags(
        [answer(a, 1, text), answer(b, 1, text + "."), answer(b, 2, "jawaban lain sama sekali")],
        CFG,
    )
    assert sorted(s for s, _ in flags) == sorted([a, b])
    assert {f.evidence["related_session_ids"][0] for _, f in flags} == {str(a), str(b)}


def test_short_identical_answers_are_not_similar() -> None:
    a, b = uuid4(), uuid4()
    assert similarity_flags([answer(a, 1, "tidak tahu"), answer(b, 1, "Tidak tahu!")], CFG) == []


def test_same_text_on_different_turns_is_not_similar() -> None:
    a, b = uuid4(), uuid4()
    text = "Kelereng berhenti karena gaya gesek antara kelereng dan lantai"
    assert similarity_flags([answer(a, 1, text), answer(b, 2, text)], CFG) == []
