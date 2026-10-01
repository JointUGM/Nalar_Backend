from types import TracebackType
from typing import Protocol, Self

from nalar.application.ports.ai import AiInvocationLog
from nalar.application.ports.audit import AuditRepo
from nalar.application.ports.authz import Authz
from nalar.application.ports.evaluations import EvaluationsRepo
from nalar.application.ports.grading import GradingRepo
from nalar.application.ports.identity import IdentityRepo
from nalar.application.ports.integrity import IntegrityRepo
from nalar.application.ports.jobs import JobStore
from nalar.application.ports.missions import MissionsRepo
from nalar.application.ports.notifications import NotificationsRepo
from nalar.application.ports.parents import ParentsRepo
from nalar.application.ports.participants import ParticipantsRepo
from nalar.application.ports.publications import PublicationsRepo
from nalar.application.ports.queue import QueueSender
from nalar.application.ports.release import ReleaseRepo
from nalar.application.ports.results import ResultsRepo
from nalar.application.ports.runs import RunsRepo
from nalar.application.ports.scheduler import SchedulerRepo
from nalar.application.ports.sessions import SessionsRepo
from nalar.application.ports.telemetry import TelemetryRepo
from nalar.application.ports.turns import TurnsRepo


class UnitOfWork(Protocol):
    """One database transaction. Enqueued messages commit or roll back with it."""

    authz: Authz
    ai_invocations: AiInvocationLog
    jobs: JobStore
    queue: QueueSender
    identity: IdentityRepo
    publications: PublicationsRepo
    runs: RunsRepo
    participants: ParticipantsRepo
    sessions: SessionsRepo
    turns: TurnsRepo
    notifications: NotificationsRepo
    telemetry: TelemetryRepo
    scheduler: SchedulerRepo
    evaluations: EvaluationsRepo
    results: ResultsRepo
    audit: AuditRepo
    grading: GradingRepo
    integrity: IntegrityRepo
    release: ReleaseRepo
    parents: ParentsRepo
    missions: MissionsRepo

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...
