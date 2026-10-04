import hashlib
import json
from email.utils import format_datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import Header, Request, Response
from pydantic import BaseModel

type PollRequestTag = Annotated[str | None, Header(alias="If-None-Match")]

POLL_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "headers": {
            "ETag": {"schema": {"type": "string"}},
            "Date": {"schema": {"type": "string"}},
            "Cache-Control": {"schema": {"type": "string"}},
        }
    },
    304: {
        "description": "Authorized view unchanged; empty body. Use Date for clock synchronization.",
        "headers": {"ETag": {"schema": {"type": "string"}}, "Date": {"schema": {"type": "string"}}},
    },
}


def conditional_response(
    body: BaseModel, request: Request, actor_id: UUID, if_none_match: str | None
) -> Response:
    data = body.model_dump(mode="json")
    # Clock synchronization changes every poll; it is carried by Date on 304 too.
    stable = {k: v for k, v in data.items() if k != "server_now"}
    digest = hashlib.sha256(
        json.dumps([str(actor_id), str(request.url), stable], sort_keys=True).encode()
    ).hexdigest()
    tag = f'W/"{digest}"'
    headers = {"ETag": tag, "Cache-Control": "private, no-cache", "Vary": "Cookie, Origin"}
    now = getattr(body, "server_now", None)
    if now is not None:
        headers["Date"] = format_datetime(now, usegmt=True)
    matches = (if_none_match or "").split(",")
    if any(value.strip().removeprefix("W/") in (tag[2:], "*") for value in matches):
        return Response(status_code=304, headers=headers)
    return Response(body.model_dump_json(), media_type="application/json", headers=headers)
