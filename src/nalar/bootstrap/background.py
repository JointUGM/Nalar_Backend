import asyncio
import logging
from collections.abc import Coroutine
from typing import Any
from uuid import UUID

from dishka import AsyncContainer

from nalar.application.features.runs.commands.warm_run import WarmRunHandler
from nalar.application.features.sessions.commands.run_turn_step import RunTurnStepHandler

log = logging.getLogger(__name__)


class InProcessBackground:
    """Runs work after the response in its own request scope, so it gets its own connection."""

    def __init__(self, container: AsyncContainer) -> None:
        self._container = container
        self._tasks: set[asyncio.Task[None]] = set()
        self._turn_steps: set[tuple[UUID, int]] = set()

    def warm_run(self, run_id: UUID) -> None:
        self._spawn(self._warm(run_id))

    def run_turn_step(self, session_id: UUID, answered_turn_index: int) -> None:
        key = (session_id, answered_turn_index)
        # The state read re-kicks a slow turn on every poll; one step per turn avoids paying twice.
        if key in self._turn_steps:
            return
        self._turn_steps.add(key)
        self._spawn(self._turn_step(key))

    def _spawn(self, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _warm(self, run_id: UUID) -> None:
        try:
            async with self._container() as scope:
                await (await scope.get(WarmRunHandler)).execute(run_id)
        except Exception:
            log.exception("background warm failed", extra={"run_id": str(run_id)})

    async def _turn_step(self, key: tuple[UUID, int]) -> None:
        session_id, answered_turn_index = key
        try:
            async with self._container() as scope:
                handler = await scope.get(RunTurnStepHandler)
                await handler.execute(session_id, answered_turn_index)
        except Exception:
            log.exception("background turn step failed", extra={"session_id": str(session_id)})
        finally:
            self._turn_steps.discard(key)
