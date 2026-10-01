from datetime import timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from nalar.application.features.sessions.commands.run_turn_step import RunTurnStepHandler
from nalar.application.ports.ai import AiResult, AiServiceError
from nalar.application.ports.ai_contract import NextTurnOut
from nalar.application.ports.sessions import StoredTurn, TurnContext
from nalar.application.ports.uow import UnitOfWork
from nalar.domain.labels import SessionEndReason, SessionStatus
from tests.unit.application.fakes import (
    SEED_PACK,
    FakeClock,
    FakeUnitOfWork,
    ScriptedAiGateway,
    invocation,
)

SESSION = uuid4()
TARGETS = [UUID(t["id"]) for t in SEED_PACK["targets"]]


def turn(index: int, answer: str | None = "Karena gayanya habis", **kw: Any) -> StoredTurn:
    base: dict[str, Any] = {
        "id": uuid4(),
        "turn_index": index,
        "kind": "anchor" if index == 0 else "probe",
        "question_text": f"Pertanyaan {index}",
        "answer_text": answer,
        "answer_state": None,
        "move": None if index == 0 else "request_justification",
        "target_concept_id": None if index == 0 else TARGETS[0],
        "question_bank_id": None,
        "detected_misconception_id": None,
        "secondary_misconception_id": None,
    }
    return StoredTurn(**(base | kw))


def setup(
    *turns: StoredTurn,
    deadline_in: timedelta = timedelta(minutes=10),
    max_turns: int = 6,
    clock: FakeClock | None = None,
) -> tuple[RunTurnStepHandler, FakeUnitOfWork, ScriptedAiGateway]:
    clock = clock or FakeClock()
    uow, ai = FakeUnitOfWork(), ScriptedAiGateway()
    uow.sessions.context = TurnContext(
        session_id=SESSION,
        school_id=uuid4(),
        publication_id=uuid4(),
        status=SessionStatus.in_progress,
        started_at=clock.now() - timedelta(minutes=2),
        deadline_at=clock.now() + deadline_in,
        max_turns=max_turns,
        context_pack=SEED_PACK,
        planner_mode="table",
        turns=turns,
    )
    uow.publications.teachers = [uuid4(), uuid4()]
    return RunTurnStepHandler(cast(UnitOfWork, uow), clock, ai), uow, ai


def reply(
    action: str = "probe",
    answer_type: str = "misconception",
    end_reason: str | None = None,
    pack: dict[str, Any] = SEED_PACK,
) -> AiResult[NextTurnOut]:
    bank = next(q for q in pack["question_bank"] if q["move"] == "counter_example")
    probe = {
        "allowed_moves": ["counter_example", "request_justification"],
        "guard_result": "passed",
        "move": "counter_example",
        "move_source": "planner",
        "question_bank_id": bank["id"],
        "question_source": "approved",
        "question_text": "Bagaimana jika lantainya licin?",
        "reason": "Menguji gagasan gaya habis",
        "reason_code": "default",
        "target_concept_id": bank["concept_id"],
    }
    body: dict[str, Any] = {
        "action": action,
        "analysis": {
            "answer_type": answer_type,
            "frustration": False,
            "key_phrase": None,
            "misconception_id": pack["misconceptions"][0]["id"],
            "secondary_misconception_id": None,
            "source": "model",
        },
        "coverage": [],
        "end_reason": end_reason,
        "safety_message": None,
        "probe": probe if action == "probe" else None,
    }
    return AiResult(
        NextTurnOut.model_validate(body), [invocation("turn_analyze"), invocation("probe_plan")]
    )


async def test_probe_writes_the_next_turn_with_its_trace() -> None:
    handler, uow, ai = setup(turn(0))
    ai.script("next_turn", reply())
    await handler.execute(SESSION, 0)
    [new] = uow.turns.appended
    assert (new.turn_index, new.move, new.move_source, new.guard_result) == (
        1,
        "counter_example",
        "planner",
        "passed",
    )
    assert new.allowed_moves == ("counter_example", "request_justification")
    [analysis] = uow.turns.analyses.values()
    assert analysis.answer_state == "misconception"
    assert len(uow.ai_invocations.recorded) == 2


@pytest.mark.parametrize(
    ("ai_reason", "status", "reason"),
    [
        ("coverage_complete", SessionStatus.completed, SessionEndReason.student_completed),
        ("turn_limit", SessionStatus.completed, SessionEndReason.max_turns_reached),
        ("time_limit", SessionStatus.timed_out, SessionEndReason.max_duration_reached),
    ],
)
async def test_each_end_reason_ends_the_session_and_queues_one_evaluation(
    ai_reason: str, status: SessionStatus, reason: SessionEndReason
) -> None:
    handler, uow, ai = setup(turn(0))
    ai.script("next_turn", reply("end", end_reason=ai_reason))
    await handler.execute(SESSION, 0)
    assert uow.sessions.ended == [(status, reason)]
    assert uow.queue.sent == [
        ("nalar_eval", {"kind": "evaluate_session", "session_id": str(SESSION)})
    ]
    assert uow.turns.appended == []


async def test_safety_pause_pauses_and_alerts_each_teacher_once() -> None:
    handler, uow, ai = setup(turn(0))
    ai.script("next_turn", reply("safety_pause", "safety"))
    await handler.execute(SESSION, 0)
    await handler.execute(SESSION, 0)
    assert uow.sessions.status is SessionStatus.paused_safety
    assert len(uow.notifications.alerts) == 2
    [analysis] = uow.turns.analyses.values()
    assert (analysis.safety_paused, analysis.answer_state) == (True, None)


async def test_turn_falls_back_to_approved_question_when_ai_times_out() -> None:
    handler, uow, ai = setup(turn(0), turn(1, target_concept_id=TARGETS[1]))
    ai.script("next_turn", AiServiceError("timeout", None))
    await handler.execute(SESSION, 1)
    [new] = uow.turns.appended
    expected = next(
        q
        for q in SEED_PACK["question_bank"]
        if q["move"] == "request_justification" and q["concept_id"] == str(TARGETS[1])
    )
    assert (new.question_bank_id, new.prompt_text) == (expected["id"], expected["text"])
    assert (new.move_source, new.guard_result, new.turn_index) == ("fallback_error", "not_run", 2)


async def test_fallback_at_the_last_turn_ends_the_session() -> None:
    handler, uow, ai = setup(turn(0), turn(1), turn(2), max_turns=2)
    ai.script(
        "next_turn",
        AiServiceError("upstream_unavailable", 503, [invocation("turn_analyze", "error")]),
    )
    await handler.execute(SESSION, 2)
    assert uow.turns.appended == []
    assert uow.sessions.ended == [(SessionStatus.completed, SessionEndReason.max_turns_reached)]
    assert len(uow.ai_invocations.recorded) == 1


async def test_history_maps_stored_labels_back_to_ai_values() -> None:
    handler, uow, ai = setup(turn(0, answer_state="manipulation_attempt"), turn(1))
    ai.script("next_turn", reply())
    await handler.execute(SESSION, 1)
    [(_, request)] = ai.calls
    history = request.model_dump(mode="json")["history"]
    assert [h["answer_type"] for h in history] == ["manipulation", None]


async def test_history_the_ai_cannot_read_uses_the_fallback() -> None:
    handler, uow, ai = setup(turn(0), turn(1, move="vary_variable"))
    await handler.execute(SESSION, 1)
    assert ai.calls == []
    assert uow.turns.appended[0].move_source == "fallback_error"


async def test_deadline_passed_times_out_without_calling_the_ai() -> None:
    handler, uow, ai = setup(turn(0), deadline_in=timedelta(seconds=-1))
    await handler.execute(SESSION, 0)
    assert ai.calls == []
    assert uow.sessions.status is SessionStatus.timed_out
    assert len(uow.queue.sent) == 1


async def test_an_already_advanced_session_is_a_no_op() -> None:
    handler, uow, ai = setup(turn(0), turn(1, answer=None))
    await handler.execute(SESSION, 0)
    assert ai.calls == [] and uow.turns.appended == []


@pytest.mark.parametrize("fallback", [False, True])
async def test_next_prompt_timestamp_excludes_ai_latency(fallback: bool) -> None:
    clock = FakeClock()
    handler, uow, ai = setup(turn(0), clock=clock)

    def delayed(_: Any) -> AiResult[NextTurnOut]:
        clock.advance(seconds=5)
        if fallback:
            raise AiServiceError("timeout", None)
        return reply()

    ai.script("next_turn", delayed)
    await handler.execute(SESSION, 0)
    assert uow.turns.appended[0].shown_at == clock.now()


@pytest.mark.parametrize("fallback", [False, True])
async def test_deadline_during_ai_call_times_out_and_keeps_provenance(fallback: bool) -> None:
    clock = FakeClock()
    handler, uow, ai = setup(turn(0), clock=clock, deadline_in=timedelta(seconds=5))

    def delayed(_: Any) -> AiResult[NextTurnOut]:
        clock.advance(seconds=5)
        if fallback:
            raise AiServiceError("timeout", None, [invocation("turn_analyze", "error")])
        return reply()

    ai.script("next_turn", delayed)
    await handler.execute(SESSION, 0)
    await handler.execute(SESSION, 0)
    assert uow.sessions.status is SessionStatus.timed_out
    assert uow.turns.appended == []
    assert len(uow.queue.sent) == 1
    assert len(uow.ai_invocations.recorded) == (1 if fallback else 2)
