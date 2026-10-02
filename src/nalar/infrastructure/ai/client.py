import json
from dataclasses import asdict, dataclass
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from nalar.application.ports.ai import AiResult, AiServiceError, ChunkSpec
from nalar.application.ports.ai_contract import (
    AlignCpIn,
    AlignCpOut,
    ChunkSectionOut,
    ClassInsightIn,
    ClassInsightOut,
    DedupeIn,
    DedupeOut,
    DetectSectionsOut,
    EmbedIn,
    EmbedOut,
    ErrorEnvelope,
    EvaluateIn,
    EvaluateOut,
    ExtractConceptsIn,
    ExtractConceptsOut,
    GenerateMisconceptionsIn,
    GenerateMisconceptionsOut,
    GenerateMissionIn,
    GenerateMissionOut,
    InvocationOut,
    NextTurnIn,
    NextTurnOut,
    ParentSummaryIn,
    ParentSummaryOut,
    SelectTargetsIn,
    SelectTargetsOut,
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
    s5_insight_s: float
    s5_summary_s: float
    s1_step_s: float
    s2_select_s: float = 100.0
    s2_generate_s: float = 500.0


class AiServiceClient:
    def __init__(self, http: httpx.AsyncClient, timeouts: AiTimeouts) -> None:
        self._http = http
        self._timeouts = timeouts

    async def select_targets(
        self, body: SelectTargetsIn, request_id: str
    ) -> AiResult[SelectTargetsOut]:
        return await self._post(
            "/v1/s2/targets/select", body, SelectTargetsOut, self._timeouts.s2_select_s, request_id
        )

    async def generate_mission(
        self, body: GenerateMissionIn, request_id: str
    ) -> AiResult[GenerateMissionOut]:
        return await self._post(
            "/v1/s2/missions/generate",
            body,
            GenerateMissionOut,
            self._timeouts.s2_generate_s,
            request_id,
        )

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

    async def class_insight(
        self, body: ClassInsightIn, request_id: str
    ) -> AiResult[ClassInsightOut]:
        return await self._post(
            "/v1/s5/class-insight", body, ClassInsightOut, self._timeouts.s5_insight_s, request_id
        )

    async def parent_summary(
        self, body: ParentSummaryIn, request_id: str
    ) -> AiResult[ParentSummaryOut]:
        return await self._post(
            "/v1/s5/parent-summaries/generate",
            body,
            ParentSummaryOut,
            self._timeouts.s5_summary_s,
            request_id,
        )

    async def detect_sections(
        self, pdf: bytes, fallback_title: str, request_id: str
    ) -> AiResult[DetectSectionsOut]:
        return await self._multipart(
            "/v1/s1/sections/detect",
            {"fallback_title": fallback_title},
            pdf,
            DetectSectionsOut,
            request_id,
        )

    async def chunk_section(
        self, pdf: bytes, spec: ChunkSpec, request_id: str
    ) -> AiResult[ChunkSectionOut]:
        return await self._multipart(
            "/v1/s1/sections/chunk",
            {"spec": json.dumps(asdict(spec))},
            pdf,
            ChunkSectionOut,
            request_id,
        )

    async def extract_concepts(
        self, body: ExtractConceptsIn, request_id: str
    ) -> AiResult[ExtractConceptsOut]:
        return await self._post(
            "/v1/s1/concepts/extract",
            body,
            ExtractConceptsOut,
            self._timeouts.s1_step_s,
            request_id,
        )

    async def dedupe_concepts(self, body: DedupeIn, request_id: str) -> AiResult[DedupeOut]:
        return await self._post(
            "/v1/s1/concepts/dedupe", body, DedupeOut, self._timeouts.s1_step_s, request_id
        )

    async def align_cp(self, body: AlignCpIn, request_id: str) -> AiResult[AlignCpOut]:
        return await self._post(
            "/v1/s1/concepts/align-cp", body, AlignCpOut, self._timeouts.s1_step_s, request_id
        )

    async def generate_misconceptions(
        self, body: GenerateMisconceptionsIn, request_id: str
    ) -> AiResult[GenerateMisconceptionsOut]:
        return await self._post(
            "/v1/s1/misconceptions/generate",
            body,
            GenerateMisconceptionsOut,
            self._timeouts.s1_step_s,
            request_id,
        )

    async def _multipart[T: BaseModel](
        self,
        path: str,
        fields: dict[str, str],
        pdf: bytes,
        result_type: type[T],
        request_id: str,
    ) -> AiResult[T]:
        return await self._send(
            path,
            result_type,
            files={"file": ("material.pdf", pdf, "application/pdf")},
            data=fields,
            headers={"X-Request-Id": request_id},
            timeout=self._timeouts.s1_step_s,
        )

    async def _post[T: BaseModel](
        self,
        path: str,
        body: BaseModel,
        result_type: type[T],
        timeout_s: float,
        request_id: str,
    ) -> AiResult[T]:
        return await self._send(
            path,
            result_type,
            # The AI rejects null for optional fields it types as non-nullable (e.g. lists).
            content=body.model_dump_json(exclude_none=True),
            headers={"Content-Type": "application/json", "X-Request-Id": request_id},
            timeout=timeout_s,
        )

    async def _send[T: BaseModel](
        self, path: str, result_type: type[T], **request: Any
    ) -> AiResult[T]:
        try:
            response = await self._http.post(path, **request)
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
