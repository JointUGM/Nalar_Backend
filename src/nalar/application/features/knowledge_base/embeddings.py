from collections.abc import Sequence
from math import isfinite
from uuid import uuid4

from nalar.application.errors import DependencyUnavailable
from nalar.application.ports.ai import AiGateway, AiServiceError
from nalar.application.ports.ai_contract import EmbedIn, InvocationOut, Tag
from nalar.application.ports.knowledge import ItemRef
from nalar.application.ports.uow import UnitOfWork


async def embed_item(
    uow: UnitOfWork, ai: AiGateway, model: str, ref: ItemRef, text: str
) -> tuple[list[float], Sequence[InvocationOut]]:
    tag = Tag.concept if ref.kind == "concept" else Tag.misconception
    body = EmbedIn.model_validate({"tag": tag, "texts": [text]})
    try:
        reply = await ai.embed(body, f"embed-{ref.id}-{uuid4()}")
    except AiServiceError as error:
        async with uow:
            await uow.ai_invocations.record(ref.school_id, error.invocations)
        raise DependencyUnavailable() from error
    result = reply.result
    if result.embedding_model != model:
        async with uow:
            await uow.ai_invocations.record(ref.school_id, reply.invocations)
        raise DependencyUnavailable("EMBEDDING_MODEL_MISMATCH")
    if (
        result.dimensions != 1536
        or len(result.vectors) != 1
        or len(result.vectors[0]) != 1536
        or not all(map(isfinite, result.vectors[0]))
    ):
        async with uow:
            await uow.ai_invocations.record(ref.school_id, reply.invocations)
        raise DependencyUnavailable("EMBEDDING_INVALID")
    return result.vectors[0], reply.invocations
