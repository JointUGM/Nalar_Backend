import json
import logging
from collections.abc import Sequence
from uuid import UUID

from nalar.application.features.evaluation.payloads import evaluate_request
from nalar.application.ports.ai import AiGateway, AiResult, AiServiceError
from nalar.application.ports.ai_contract import EvaluateOut, InvocationOut
from nalar.application.ports.evaluations import ConceptResultRow, EvaluationInput
from nalar.application.ports.uow import UnitOfWork
from nalar.domain.context_pack import target_ids_from_pack
from nalar.domain.evaluation import OutcomeCheck, ScoreCheck, output_problems
from nalar.domain.labels import EvaluationStatus
from nalar.domain.sessions import TERMINAL_STATUSES

log = logging.getLogger(__name__)
_RETRYABLE_ONCE = frozenset({500, 502})


class EvaluateSessionHandler:
    """Master plan §6.3 without E1. The unique session_evaluations row is the exactly-once guard.

    A 503, timeout or unreachable service re-raises after recording the invocations, so the
    queue re-delivers the message after its visibility timeout (B7).
    """

    def __init__(self, uow: UnitOfWork, ai: AiGateway) -> None:
        self._uow = uow
        self._ai = ai

    async def execute(self, session_id: UUID) -> None:
        async with self._uow:
            data = await self._uow.evaluations.evaluation_input(session_id)
        if data is None or data.evaluated or data.status not in TERMINAL_STATUSES:
            return
        request = evaluate_request(data)
        context = {"session_id": str(session_id)}
        for attempt in (1, 2):
            try:
                reply = await self._ai.evaluate_session(
                    request, request_id=f"eval-{session_id}-{attempt}"
                )
            except AiServiceError as error:
                await self._record(data, error.invocations)
                if error.http_status == 422:
                    await self._terminal(data, EvaluationStatus.no_answer)
                    return
                if error.http_status in _RETRYABLE_ONCE or error.code == "bad_response":
                    log.warning(
                        "evaluation attempt failed",
                        extra=context | {"code": error.code, "attempt": attempt},
                    )
                    continue
                raise
            problems = _problems(data, reply.result)
            if problems:
                log.warning(
                    "evaluation output rejected",
                    extra=context | {"problems": problems, "attempt": attempt},
                )
                await self._record(data, reply.invocations)
                continue
            await self._complete(data, reply)
            return
        await self._terminal(data, EvaluationStatus.failed)

    async def _record(self, data: EvaluationInput, invocations: Sequence[InvocationOut]) -> None:
        async with self._uow:
            await self._uow.ai_invocations.record(data.school_id, invocations)

    async def _terminal(self, data: EvaluationInput, status: EvaluationStatus) -> None:
        async with self._uow:
            await self._uow.evaluations.insert(
                data.session_id, data.school_id, status, None, "[]", None
            )

    async def _complete(self, data: EvaluationInput, reply: AiResult[EvaluateOut]) -> None:
        result, repo = reply.result, self._uow.evaluations
        async with self._uow:
            ids = await self._uow.ai_invocations.record(data.school_id, reply.invocations)
            by_purpose = {
                inv.purpose.value: id_ for inv, id_ in zip(reply.invocations, ids, strict=True)
            }
            evaluation_id = await repo.insert(
                data.session_id,
                data.school_id,
                EvaluationStatus.completed,
                result.summary,
                json.dumps([q.model_dump(mode="json") for q in result.turn_quality]),
                by_purpose.get("session_evaluation"),
            )
            if evaluation_id is None:
                return
            for score in result.scores:
                score_id = await repo.insert_score(
                    data.school_id,
                    evaluation_id,
                    score.dimension.value,
                    score.level,
                    score.rationale,
                )
                for evidence in score.evidence:
                    await repo.insert_evidence(
                        data.school_id, score_id, evidence.turn_id, evidence.quote
                    )
            for r in result.concept_results:
                await repo.insert_concept_result(
                    data.school_id,
                    data.session_id,
                    ConceptResultRow(
                        r.concept_id,
                        r.outcome.value,
                        r.misconception_id,
                        r.initial_misconception_id,
                        r.resolved_in_session,
                        r.evidence_turn_id,
                    ),
                )
            await repo.insert_reflection(
                data.school_id,
                data.session_id,
                result.reflection.content,
                by_purpose.get("reflection_generation"),
            )


def _problems(data: EvaluationInput, result: EvaluateOut) -> list[str]:
    pack = data.context_pack
    return output_problems(
        {t.id: t.answer_text for t in data.turns if t.answer_text},
        [
            ScoreCheck(s.dimension.value, s.level, tuple((e.turn_id, e.quote) for e in s.evidence))
            for s in result.scores
        ],
        [
            OutcomeCheck(
                r.concept_id,
                r.outcome.value,
                r.misconception_id,
                r.initial_misconception_id,
                r.resolved_in_session,
                r.evidence_turn_id,
            )
            for r in result.concept_results
        ],
        turn_ids={t.id for t in data.turns},
        target_ids=set(target_ids_from_pack(pack)),
        misconception_ids={UUID(str(m["id"])) for m in pack.get("misconceptions") or []},
    )
