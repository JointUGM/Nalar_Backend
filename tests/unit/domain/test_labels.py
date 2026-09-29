from nalar.application.ports.ai_contract import AnswerType, EndReason, ProbeStrategy
from nalar.domain.labels import (
    AI_TYPE_BY_ANSWER_STATE,
    ANSWER_STATE_BY_AI_TYPE,
    PROBE_MOVES,
    SESSION_END_BY_AI_REASON,
    SessionEndReason,
    SessionStatus,
)


def test_every_ai_answer_type_has_a_database_mapping() -> None:
    assert set(ANSWER_STATE_BY_AI_TYPE) == {t.value for t in AnswerType}
    assert ANSWER_STATE_BY_AI_TYPE["manipulation"] == "manipulation_attempt"
    assert ANSWER_STATE_BY_AI_TYPE["unsure"] is None
    assert ANSWER_STATE_BY_AI_TYPE["safety"] is None


def test_stored_answer_states_map_back_to_ai_values() -> None:
    for ai_value, state in ANSWER_STATE_BY_AI_TYPE.items():
        if state is not None:
            assert AI_TYPE_BY_ANSWER_STATE[state] == ai_value


def test_every_ai_end_reason_maps_to_a_terminal_status() -> None:
    assert set(SESSION_END_BY_AI_REASON) == {r.value for r in EndReason}
    assert SESSION_END_BY_AI_REASON["time_limit"] == (
        SessionStatus.timed_out,
        SessionEndReason.max_duration_reached,
    )


def test_probe_moves_match_the_ai_move_set() -> None:
    assert set(PROBE_MOVES) == {m.value for m in ProbeStrategy}
