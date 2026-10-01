from dataclasses import dataclass
from datetime import timedelta

from nalar.application.features.evaluation.messages import evaluation_message
from nalar.application.features.release.messages import FINALIZE_KIND, finalize_message
from nalar.application.ports.background import BackgroundWork
from nalar.application.ports.clock import Clock
from nalar.application.ports.queue import DEFAULT_QUEUE, EVAL_QUEUE
from nalar.application.ports.uow import UnitOfWork


@dataclass(frozen=True)
class SchedulerTiming:
    recovery_after: timedelta
    evaluation_sweep_after: timedelta
    finalize_cooldown: timedelta


@dataclass(frozen=True)
class TickReport:
    opened: int
    closed: int
    timed_out: int
    recovered: int
    swept: int
    finalizing: int = 0


class TickHandler:
    """Master plan §6.6 steps 1–5; every step is guarded, so a repeated tick changes nothing."""

    def __init__(
        self, uow: UnitOfWork, clock: Clock, background: BackgroundWork, timing: SchedulerTiming
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._background = background
        self._timing = timing

    async def execute(self) -> TickReport:
        now = self._clock.now()
        async with self._uow:
            opened = await self._uow.scheduler.open_due_windows(now)
            closed = await self._uow.scheduler.close_due_windows(now)
        async with self._uow:
            timed_out = await self._uow.scheduler.time_out_overdue(now)
            for session_id in timed_out:
                await self._uow.queue.send(EVAL_QUEUE, evaluation_message(session_id))
        # The timeouts above are committed with their messages, so the sweep below skips them.
        async with self._uow:
            stuck = await self._uow.scheduler.stuck_turns(now - self._timing.recovery_after)
            swept = await self._uow.scheduler.unevaluated_sessions(
                now - self._timing.evaluation_sweep_after
            )
            for session_id in swept:
                await self._uow.queue.send(EVAL_QUEUE, evaluation_message(session_id))
        async with self._uow:
            settled = await self._uow.scheduler.publications_to_finalize(
                now - self._timing.finalize_cooldown
            )
            for publication_id, school_id in settled:
                job_id = await self._uow.jobs.create(
                    kind=FINALIZE_KIND,
                    entity_type="publications",
                    entity_id=publication_id,
                    school_id=school_id,
                    requested_by=None,
                )
                await self._uow.queue.send(DEFAULT_QUEUE, finalize_message(publication_id, job_id))
        for session_id, turn_index in stuck:
            self._background.run_turn_step(session_id, turn_index)
        return TickReport(
            len(opened), len(closed), len(timed_out), len(stuck), len(swept), len(settled)
        )
