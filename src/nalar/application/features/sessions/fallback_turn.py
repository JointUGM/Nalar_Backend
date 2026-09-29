from datetime import datetime

from nalar.application.features.evaluation.messages import evaluation_message
from nalar.application.features.sessions.turn_payloads import last_target_id
from nalar.application.ports.queue import EVAL_QUEUE
from nalar.application.ports.sessions import StoredTurn, TurnContext
from nalar.application.ports.turns import NewTurn
from nalar.application.ports.uow import UnitOfWork
from nalar.domain.context_pack import question_bank_from_pack
from nalar.domain.fallback import fallback_question
from nalar.domain.labels import SessionEndReason, SessionStatus


async def append_fixed_question(
    uow: UnitOfWork, *, ctx: TurnContext, answered: StoredTurn, move_source: str, now: datetime
) -> None:
    """NFR-R4 fallback turn, or the session's end when no turn is left.

    Call inside the caller's transaction, after lock_if_in_progress succeeded.
    """
    next_index = answered.turn_index + 1
    if next_index > ctx.max_turns:
        if await uow.sessions.end(
            ctx.session_id, SessionStatus.completed, SessionEndReason.max_turns_reached, now
        ):
            await uow.queue.send(EVAL_QUEUE, evaluation_message(ctx.session_id))
        return
    question = fallback_question(
        question_bank_from_pack(ctx.context_pack), last_target_id(ctx, answered)
    )
    await uow.turns.append(
        ctx.session_id,
        ctx.school_id,
        NewTurn(
            turn_index=next_index,
            prompt_text=question.text,
            move=question.move,
            move_source=move_source,
            move_reason_code="default",
            move_reason=None,
            guard_result="not_run",
            question_bank_id=question.id,
            target_concept_id=question.concept_id,
            allowed_moves=None,
            ai_invocation_id=None,
            shown_at=now,
        ),
    )
