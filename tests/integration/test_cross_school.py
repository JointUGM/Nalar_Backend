import json
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest

from tests.integration.support.api import api_client, as_user
from tests.integration.support.factories import World, add_flag, add_score, build_world

AT = "2026-10-08T02:00:00Z"
PUBLISH = {"mission_version_id": "{version}", "class_id": "{klass}", "run": {"mode": "live"}}
ANSWER = {"turn_index": 0, "answer_text": "x", "client_submission_id": "{uuid}"}
TELEMETRY = {"client_seq": 1, "events": [{"type": "disconnect", "at": AT}]}
ROUTES: list[tuple[str, str, str, dict[str, Any] | None]] = [
    ("teacher", "post", "/runs/{run}/open-lobby", None),
    ("teacher", "post", "/runs/{run}/start", None),
    ("teacher", "post", "/runs/{run}/close", None),
    ("teacher", "get", "/publications/{publication}/monitor", None),
    ("teacher", "get", "/publications/{publication}/class-map", None),
    ("teacher", "get", "/sessions/{session}/report", None),
    ("teacher", "post", "/sessions/{session}/safety-actions", {"action": "end"}),
    ("teacher", "get", "/teacher/publications?class_id={klass}", None),
    ("teacher", "post", "/publications", PUBLISH),
    ("teacher", "get", "/publications/{publication}/release-preview", None),
    ("teacher", "post", "/publications/{publication}/release", {"expected_eligible_count": 0}),
    ("teacher", "post", "/scores/{score}/overrides", {"final_level": 3, "reason": "x"}),
    ("teacher", "post", "/flags/{flag}/review", {"decision": "cleared"}),
    ("parent", "get", "/parent/children/{child}/progress", None),
    ("parent", "get", "/parent/children/{child}/reflections", None),
    ("student", "get", "/student/runs/{run}/lobby", None),
    ("student", "put", "/student/runs/{run}/warmup-choice", {"choice_id": "a"}),
    ("student", "post", "/student/publications/{publication}/window-session", None),
    ("student", "post", "/student/sessions/{session}/answers", ANSWER),
    ("student", "get", "/student/sessions/{session}/state", None),
    ("student", "post", "/student/sessions/{session}/telemetry", TELEMETRY),
    ("student", "get", "/student/sessions/{session}/reflection", None),
]


def fill(text: str, victim: World, extra: dict[str, UUID]) -> str:
    values: dict[str, Any] = {
        **extra,
        "run": victim.run_id,
        "publication": victim.publication_id,
        "session": victim.session_id,
        "klass": victim.class_id,
        "version": victim.version_id,
        "uuid": uuid4(),
    }
    for name, value in values.items():
        text = text.replace("{" + name + "}", str(value))
    return text


@pytest.mark.parametrize(
    ("actor", "method", "path", "body"), ROUTES, ids=[f"{m} {p}" for _, m, p, _ in ROUTES]
)
async def test_cross_school_ids_return_404(
    conn: asyncpg.Connection,
    world: World,
    actor: str,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    extra = {
        "score": await add_score(conn, world, world.session_id),
        "flag": await add_flag(conn, world, world.session_id),
        "child": world.student_id,
    }
    intruder = await build_world(conn, "SMP Penyusup")
    user = {
        "teacher": intruder.teacher_id,
        "student": intruder.student_id,
        "parent": intruder.parent_id,
    }[actor]
    kwargs = {"json": json.loads(fill(json.dumps(body), world, extra))} if body else {}
    async with api_client(conn) as api:
        response = await getattr(api, method)(
            fill(path, world, extra), headers=as_user(user), **kwargs
        )
    assert response.status_code == 404
    for victim_id in (
        world.run_id,
        world.publication_id,
        world.session_id,
        world.class_id,
        *extra.values(),
    ):
        assert str(victim_id) not in response.text
