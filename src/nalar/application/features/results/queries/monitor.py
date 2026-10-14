from dataclasses import replace
from uuid import UUID

from nalar.application.errors import NotFound
from nalar.application.ports.clock import Clock
from nalar.application.ports.results import Monitor
from nalar.application.ports.uow import UnitOfWork
from nalar.domain.mentoring import help_signal


class MonitorQuery:
    def __init__(self, uow: UnitOfWork, clock: Clock, help_limit: int) -> None:
        self._uow = uow
        self._clock = clock
        self._help_limit = help_limit

    async def execute(self, actor_id: UUID, publication_id: UUID) -> Monitor:
        async with self._uow:
            if not await self._uow.authz.teaches_publication(actor_id, publication_id):
                raise NotFound()
            monitor = await self._uow.results.monitor(publication_id)
            if monitor is None:
                raise NotFound()
            session_students = {
                s.session_id: s.student_id
                for s in monitor.students
                if s.session_id is not None and s.status == "in_progress"
            }
            if monitor.run.status == "closed" or not session_students:
                return monitor
            inputs = await self._uow.results.monitor_help_inputs(
                publication_id, monitor.run.id, list(session_students)
            )
            priority = {"frustration": 0, "repeated_evasive": 1, "persistent_misconception": 2}
            candidates = [
                (context, signal)
                for context in inputs
                if (signal := help_signal(context, self._clock.now())) is not None
            ]
            candidates.sort(
                key=lambda item: (
                    priority[item[1].reason],
                    item[0].turns[0].submitted_at,
                    session_students[item[0].session_id],
                )
            )
            selected = {
                context.session_id: replace(signal, rank=rank)
                for rank, (context, signal) in enumerate(candidates[: self._help_limit], 1)
            }
            monitor = replace(
                monitor,
                students=tuple(
                    replace(
                        student,
                        help_signal=selected.get(student.session_id)
                        if student.session_id is not None
                        else None,
                    )
                    for student in monitor.students
                ),
            )
        return monitor
