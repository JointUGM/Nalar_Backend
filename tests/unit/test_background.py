import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast
from uuid import UUID, uuid4

from dishka import AsyncContainer

from nalar.bootstrap.background import InProcessBackground


class GatedStep:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, int]] = []
        self.release = asyncio.Event()

    async def execute(self, session_id: UUID, answered_turn_index: int) -> None:
        self.calls.append((session_id, answered_turn_index))
        await self.release.wait()


class StubScope:
    def __init__(self, step: GatedStep) -> None:
        self._step = step

    async def get(self, _: Any) -> GatedStep:
        return self._step


def container_for(step: GatedStep) -> AsyncContainer:
    @asynccontextmanager
    async def scope() -> AsyncIterator[StubScope]:
        yield StubScope(step)

    return cast(AsyncContainer, scope)


async def test_a_turn_step_already_in_flight_is_not_started_again() -> None:
    step, session = GatedStep(), uuid4()
    background = InProcessBackground(container_for(step))
    background.run_turn_step(session, 1)
    await asyncio.sleep(0)
    background.run_turn_step(session, 1)
    step.release.set()
    await asyncio.sleep(0.01)
    background.run_turn_step(session, 1)
    await asyncio.sleep(0.01)
    assert step.calls == [(session, 1), (session, 1)]
