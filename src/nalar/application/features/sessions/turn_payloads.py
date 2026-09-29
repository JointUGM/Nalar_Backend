from datetime import datetime
from uuid import UUID

from nalar.application.ports.ai_contract import (
    AnswerType,
    ContextPackIn,
    HistoryTurnIn,
    NextTurnIn,
    PlannerMode,
    ProbeStrategy,
    QuestionBankId,
    TurnKind,
)
from nalar.application.ports.sessions import StoredTurn, TurnContext
from nalar.domain.context_pack import target_ids_from_pack
from nalar.domain.labels import AI_TYPE_BY_ANSWER_STATE


def history_turn(turn: StoredTurn, *, latest: bool) -> HistoryTurnIn:
    """Raises ValueError for a stored label the AI no longer accepts (e.g. v1.0 vary_variable)."""
    answer_type = (
        None if latest or turn.answer_state is None else AI_TYPE_BY_ANSWER_STATE[turn.answer_state]
    )
    misconceptions = [
        m for m in (turn.detected_misconception_id, turn.secondary_misconception_id) if m
    ]
    return HistoryTurnIn(
        turn_index=turn.turn_index,
        kind=TurnKind(turn.kind),
        question_text=turn.question_text,
        answer_text=turn.answer_text or "",
        answer_type=AnswerType(answer_type) if answer_type else None,
        move=ProbeStrategy(turn.move) if turn.move else None,
        target_concept_id=turn.target_concept_id,
        question_bank_id=QuestionBankId(turn.question_bank_id) if turn.question_bank_id else None,
        misconception_ids=misconceptions or None,
    )


def next_turn_request(ctx: TurnContext, answered_index: int, now: datetime) -> NextTurnIn:
    """Raises ValueError (pydantic's ValidationError included) when the stored data is invalid."""
    return NextTurnIn(
        context_pack=ContextPackIn.model_validate(ctx.context_pack),
        elapsed_seconds=max(0, int((now - ctx.started_at).total_seconds())),
        history=[
            history_turn(t, latest=t.turn_index == answered_index)
            for t in ctx.turns
            if t.turn_index <= answered_index
        ],
        planner_mode=PlannerMode(ctx.planner_mode),
    )


def last_target_id(ctx: TurnContext, answered: StoredTurn) -> UUID | None:
    for t in sorted(ctx.turns, key=lambda t: t.turn_index, reverse=True):
        if t.turn_index <= answered.turn_index and t.target_concept_id is not None:
            return t.target_concept_id
    targets = target_ids_from_pack(ctx.context_pack)
    return targets[0] if targets else None
