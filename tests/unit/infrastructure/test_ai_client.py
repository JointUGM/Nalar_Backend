import json
from typing import Any

import httpx
import pytest

from nalar.application.ports.ai import AiServiceError
from nalar.application.ports.ai_contract import (
    CallStatus,
    ContextPackIn,
    EmbedIn,
    HistoryTurnIn,
    NextTurnIn,
    ParentSummaryIn,
    PlannerMode,
    Tag,
    Text,
    TurnKind,
)
from nalar.infrastructure.ai.client import AiServiceClient, AiTimeouts
from tests.unit.application.fakes import SEED_PACK

INVOCATION: dict[str, Any] = {
    "purpose": "embedding",
    "model": "text-embedding-3-small",
    "prompt_version": "n/a",
    "provider": "sumopod",
    "status": "success",
    "input_tokens": 12,
    "output_tokens": 0,
    "cache_read_tokens": 0,
    "cache_write_tokens": 0,
    "latency_ms": 120,
    "cost_usd": 0.0000024,
    "request_id": "req-1",
    "error_message": None,
    "retrieval": [],
}

EMBED = EmbedIn(tag=Tag.query, texts=[Text("gaya gesek")])


def make_client(handler: Any) -> AiServiceClient:
    http = httpx.AsyncClient(
        base_url="http://ai.test",
        headers={"X-Service-Key": "key"},
        transport=httpx.MockTransport(handler),
    )
    return AiServiceClient(
        http,
        AiTimeouts(
            turn_s=6, warm_s=10, evaluate_s=330, embed_s=30, s5_insight_s=190, s5_summary_s=90
        ),
    )


async def test_success_envelope_returns_result_and_invocations() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = {"embedding_model": "text-embedding-3-small", "dimensions": 1536, "vectors": [[0.1]]}
        return httpx.Response(
            200, json={"result": body, "invocations": [INVOCATION], "warnings": []}
        )

    result = await make_client(handler).embed(EMBED, request_id="req-1")

    assert result.result.dimensions == 1536
    assert result.invocations[0].status is CallStatus.success
    assert seen[0].url.path == "/v1/embeddings"
    assert seen[0].headers["X-Service-Key"] == "key"
    assert seen[0].headers["X-Request-Id"] == "req-1"


async def test_error_envelope_raises_with_the_paid_invocations() -> None:
    failed = {**INVOCATION, "status": "error", "error_message": "provider 503"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={
                "error": {"code": "upstream_unavailable", "message": "down", "details": {}},
                "request_id": "req-1",
                "invocations": [failed],
            },
        )

    with pytest.raises(AiServiceError) as caught:
        await make_client(handler).embed(EMBED, request_id="req-1")

    assert caught.value.code == "upstream_unavailable"
    assert caught.value.http_status == 503
    assert [i.status for i in caught.value.invocations] == [CallStatus.error]


async def test_timeout_is_reported_without_invocations() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(AiServiceError) as caught:
        await make_client(handler).embed(EMBED, request_id="req-1")

    assert (caught.value.code, caught.value.http_status, caught.value.invocations) == (
        "timeout",
        None,
        [],
    )


async def test_connection_failure_is_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(AiServiceError) as caught:
        await make_client(handler).embed(EMBED, request_id="req-1")

    assert caught.value.code == "unreachable"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"result": {"dimensions": "many"}, "invocations": [INVOCATION]}),
        httpx.Response(500, json={"detail": "not our envelope", "invocations": [INVOCATION]}),
    ],
)
async def test_bad_response_keeps_the_paid_invocations(response: httpx.Response) -> None:
    with pytest.raises(AiServiceError) as caught:
        await make_client(lambda request: response).embed(EMBED, request_id="req-1")

    assert caught.value.code == "bad_response"
    assert [i.purpose.value for i in caught.value.invocations] == ["embedding"]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(502, text="<html>bad gateway</html>"),
        httpx.Response(200, json={"unexpected": True}),
        httpx.Response(500, json={"detail": "not our envelope"}),
    ],
)
async def test_malformed_responses_are_bad_response(response: httpx.Response) -> None:
    with pytest.raises(AiServiceError) as caught:
        await make_client(lambda request: response).embed(EMBED, request_id="req-1")

    assert caught.value.code == "bad_response"


async def test_unset_optional_fields_are_omitted_not_sent_as_null() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(503, json={"error": {"code": "x", "message": "", "details": {}}})

    history = HistoryTurnIn(turn_index=0, kind=TurnKind.anchor, question_text="Q", answer_text="A")
    body = NextTurnIn(
        context_pack=ContextPackIn.model_validate(SEED_PACK),
        elapsed_seconds=10,
        history=[history],
        planner_mode=PlannerMode.table,
    )
    with pytest.raises(AiServiceError):
        await make_client(handler).next_turn(body, request_id="req-1")

    assert "misconception_ids" not in seen[0]["history"][0]


async def test_parent_summary_posts_to_the_s5_route_without_null_fields() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "result": {"content": "Ananda menjelaskan gaya gesek.", "source": "model"},
                "invocations": [],
                "warnings": [],
            },
        )

    body = ParentSummaryIn.model_validate(
        {"mission_title": "Gaya", "concepts": [{"name": "Gaya gesek", "outcome": "mastered"}]}
    )
    reply = await make_client(handler).parent_summary(body, "r1")
    assert seen["path"] == "/v1/s5/parent-summaries/generate"
    assert "evaluation_summary" not in seen["body"]
    assert reply.result.source.value == "model"
