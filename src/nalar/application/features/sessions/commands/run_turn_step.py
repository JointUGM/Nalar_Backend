import logging
from collections.abc import Sequence
from uuid import UUID

from nalar.application.features.evaluation.messages import evaluation_message
from nalar.application.features.sessions.fallback_turn import append_fixed_question
from nalar.application.features.sessions.turn_payloads import next_turn_request
from nalar.application.ports.ai import AiGateway, AiResult, AiServiceError
from nalar.application.ports.ai_contract import InvocationOut, NextTurnOut, TurnAction
from nalar.application.ports.clock import Clock
from nalar.application.ports.queue import EVAL_QUEUE
from nalar.application.ports.sessions import StoredTurn, TurnContext
from nalar.application.ports.turns import NewTurn, TurnAnalysis
from nalar.application.ports.uow import UnitOfWork
from nalar.domain.labels import ANSWER_STATE_BY_AI_TYPE, SESSION_END_BY_AI_REASON, SessionStatus

log = logging.getLogger(__name__)


class RunTurnStepHandler:
    """Master plan §6.2. Idempotent: re-running it for an advanced turn does nothing."""

    def __init__(self, uow: UnitOfWork, clock: Clock, ai: AiGateway) -> None:
        self._uow = uow
        self._clock = clock
        self._ai = ai

    async def execute(self, session_id: UUID, answered_turn_index: int) -> None:
        async with self._uow:
            ctx = await self._uow.sessions.turn_context(session_id)
        if ctx is None or ctx.status is not SessionStatus.in_progress:
            return
        answered = next((t for t in ctx.turns if t.turn_index == answered_turn_index), None)
        if answered is None or answered.answer_text is None:
            return
        if any(t.turn_index > answered_turn_index for t in ctx.turns):
            return
        now = self._clock.now()
        if now >= ctx.deadline_at:
            async with self._uow:
                if await self._uow.sessions.time_out(session_id, now):
                    await self._uow.queue.send(EVAL_QUEUE, evaluation_message(session_id))
            return
        context = {"session_id": str(session_id), "turn_index": answered_turn_index}
        try:
            request = next_turn_request(ctx, answered_turn_index, now)
        except ValueError:
            log.exception("turn history cannot be sent to the AI", extra=context)
            await self._fallback(ctx, answered, [])
            return
        # No connection is held here: the read above committed, the write below opens a new one.
        try:
            reply = await self._ai.next_turn(
                request, request_id=f"turn-{session_id}-{answered_turn_index}"
            )
        except AiServiceError as error:
            log.warning(
                "turn step fell back: %s %s %s",
                error.code,
                error.http_status,
                error.message[:200],
                extra=context | {"code": error.code},
            )
            await self._fallback(ctx, answered, error.invocations)
            return
        await self._apply(ctx, answered, reply)

    async def _still_open(self, ctx: TurnContext, answered: StoredTurn) -> bool:
        return await self._uow.sessions.lock_if_in_progress(
            ctx.session_id
        ) and not await self._uow.turns.exists(ctx.session_id, answered.turn_index + 1)

    async def _fallback(
        self,
        ctx: TurnContext,
        answered: StoredTurn,
        invocations: Sequence[InvocationOut],
    ) -> None:
        async with self._uow:
            await self._uow.ai_invocations.record(ctx.school_id, invocations)
            if await self._still_open(ctx, answered):
                now = self._clock.now()
                if await self._uow.sessions.time_out(ctx.session_id, now):
                    await self._uow.queue.send(EVAL_QUEUE, evaluation_message(ctx.session_id))
                    return
                await append_fixed_question(
                    self._uow, ctx=ctx, answered=answered, move_source="fallback_error", now=now
                )

    async def _apply(
        self, ctx: TurnContext, answered: StoredTurn, reply: AiResult[NextTurnOut]
    ) -> None:
        result = reply.result
        async with self._uow:
            ids = await self._uow.ai_invocations.record(ctx.school_id, reply.invocations)
            if not await self._still_open(ctx, answered):
                return
            now = self._clock.now()
            by_purpose = {
                inv.purpose.value: id_ for inv, id_ in zip(reply.invocations, ids, strict=True)
            }
            safety = result.action is TurnAction.safety_pause
            await self._uow.turns.record_analysis(
                answered.id,
                TurnAnalysis(
                    answer_state=ANSWER_STATE_BY_AI_TYPE[result.analysis.answer_type.value],
                    detected_misconception_id=result.analysis.misconception_id,
                    secondary_misconception_id=result.analysis.secondary_misconception_id,
                    frustration=result.analysis.frustration,
                    safety_paused=safety,
                    ai_invocation_id=by_purpose.get("turn_analyze"),
                ),
            )
            if safety:
                if await self._uow.sessions.pause_for_safety(ctx.session_id):
                    teachers = await self._uow.publications.teacher_ids(ctx.publication_id)
                    await self._uow.notifications.wellbeing_alert(
                        teachers,
                        ctx.school_id,
                        ctx.session_id,
                        ctx.publication_id,
                        answered.turn_index,
                    )
                return
            if await self._uow.sessions.time_out(ctx.session_id, now):
                await self._uow.queue.send(EVAL_QUEUE, evaluation_message(ctx.session_id))
                return
            next_index = answered.turn_index + 1
            probe = result.probe
            if result.action is TurnAction.end or probe is None or next_index > ctx.max_turns:
                ai_reason = result.end_reason.value if result.end_reason else "turn_limit"
                status, reason = SESSION_END_BY_AI_REASON[ai_reason]
                if await self._uow.sessions.end(ctx.session_id, status, reason, now):
                    await self._uow.queue.send(EVAL_QUEUE, evaluation_message(ctx.session_id))
                return
            await self._uow.turns.append(
                ctx.session_id,
                ctx.school_id,
                NewTurn(
                    turn_index=next_index,
                    prompt_text=probe.question_text,
                    move=probe.move.value,
                    move_source=probe.move_source.value,
                    move_reason_code=probe.reason_code.value,
                    move_reason=probe.reason,
                    guard_result=probe.guard_result.value,
                    question_bank_id=probe.question_bank_id,
                    target_concept_id=probe.target_concept_id,
                    allowed_moves=tuple(m.value for m in probe.allowed_moves),
                    ai_invocation_id=by_purpose.get("probe_plan"),
                    shown_at=now,
                ),
            )
