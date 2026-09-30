from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from nalar.application.ports.ai import AiResult, AiServiceError
from nalar.application.ports.ai_contract import (
    EmbedIn,
    EmbedOut,
    ErrorEnvelope,
    EvaluateIn,
    EvaluateOut,
    InvocationOut,
    NextTurnIn,
    NextTurnOut,
    WarmIn,
    WarmOut,
)


# AI-6: a rejected response may still carry billed invocations, which must be persisted.
def _paid_invocations(payload: Any) -> list[InvocationOut]:
    raw = payload.get("invocations") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []
    paid: list[InvocationOut] = []
    for item in raw:
        try:
            paid.append(InvocationOut.model_validate(item))
        except ValidationError:
            continue
    return paid


@dataclass(frozen=True)
class AiTimeouts:
    turn_s: float
    warm_s: float
    evaluate_s: float
    embed_s: float


class AiServiceClient:
    def __init__(self, http: httpx.AsyncClient, timeouts: AiTimeouts) -> None:
        self._http = http
        self._timeouts = timeouts

    async def next_turn(self, body: NextTurnIn, request_id: str) -> AiResult[NextTurnOut]:
        return await self._post(
            "/v1/s3/turns/next", body, NextTurnOut, self._timeouts.turn_s, request_id
        )

    async def warm_run(self, body: WarmIn, request_id: str) -> AiResult[WarmOut]:
        return await self._post(
            "/v1/s3/runs/warm", body, WarmOut, self._timeouts.warm_s, request_id
        )

    async def evaluate_session(self, body: EvaluateIn, request_id: str) -> AiResult[EvaluateOut]:
        return await self._post(
            "/v1/s4/sessions/evaluate", body, EvaluateOut, self._timeouts.evaluate_s, request_id
        )

    async def embed(self, body: EmbedIn, request_id: str) -> AiResult[EmbedOut]:
        return await self._post(
            "/v1/embeddings", body, EmbedOut, self._timeouts.embed_s, request_id
        )

    async def _post[T: BaseModel](
        self,
        path: str,
        body: BaseModel,
        result_type: type[T],
        timeout_s: float,
        request_id: str,
    ) -> AiResult[T]:
        try:
            response = await self._http.post(
                path,
                # The AI rejects null for optional fields it types as non-nullable (e.g. lists).
                content=body.model_dump_json(exclude_none=True),
                headers={"Content-Type": "application/json", "X-Request-Id": request_id},
                timeout=timeout_s,
            )
        except httpx.TimeoutException as exc:
            raise AiServiceError("timeout", None, message=str(exc)) from exc
        except httpx.TransportError as exc:
            raise AiServiceError("unreachable", None, message=str(exc)) from exc

        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise AiServiceError("bad_response", response.status_code, message="not JSON") from exc

        if response.is_success:
            try:
                return AiResult(
                    result=result_type.model_validate(payload["result"]),
                    invocations=[InvocationOut.model_validate(i) for i in payload["invocations"]],
                    warnings=[str(w) for w in payload.get("warnings", [])],
                )
            except (KeyError, TypeError, ValidationError) as exc:
                raise AiServiceError(
                    "bad_response", response.status_code, _paid_invocations(payload), str(exc)
                ) from exc

        try:
            envelope = ErrorEnvelope.model_validate(payload)
        except ValidationError as exc:
            raise AiServiceError(
                "bad_response", response.status_code, _paid_invocations(payload), str(exc)
            ) from exc
        raise AiServiceError(
            envelope.error.code,
            response.status_code,
            envelope.invocations,
            envelope.error.message,
        )
