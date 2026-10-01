from collections.abc import AsyncIterator
from datetime import timedelta

import asyncpg
import httpx
import redis.asyncio as aioredis
from dishka import (
    AsyncContainer,
    Provider,
    Scope,
    alias,
    make_async_container,
    provide,
    provide_all,
)

from nalar.application.features.evaluation.commands.evaluate_session import (
    EvaluateSessionHandler,
)
from nalar.application.features.identity.queries.me import MeQuery
from nalar.application.features.integrity.commands.compute_publication_similarity import (
    ComputePublicationSimilarityHandler,
)
from nalar.application.features.integrity.commands.compute_session_flags import (
    ComputeSessionFlagsHandler,
)
from nalar.application.features.parents.commands.set_preferences import SetPreferencesHandler
from nalar.application.features.parents.queries.children import ChildrenQuery
from nalar.application.features.parents.queries.preferences import PreferencesQuery
from nalar.application.features.parents.queries.progress import ProgressQuery
from nalar.application.features.parents.queries.reflections import ParentReflectionsQuery
from nalar.application.features.publications.commands.publish import PublishDefaults, PublishHandler
from nalar.application.features.publications.queries.teacher_assignments import (
    TeacherAssignmentsQuery,
)
from nalar.application.features.publications.queries.teacher_publications import (
    TeacherPublicationsQuery,
)
from nalar.application.features.release.commands.finalize_publication import (
    FinalizePublicationHandler,
)
from nalar.application.features.release.commands.release import ReleaseHandler
from nalar.application.features.release.commands.release_reminders import (
    ReleaseRemindersHandler,
)
from nalar.application.features.release.queries.preview import ReleasePreviewQuery
from nalar.application.features.results.commands.override_score import OverrideScoreHandler
from nalar.application.features.results.commands.review_flag import ReviewFlagHandler
from nalar.application.features.results.queries.class_map import ClassMapQuery
from nalar.application.features.results.queries.monitor import MonitorQuery
from nalar.application.features.results.queries.session_report import SessionReportQuery
from nalar.application.features.runs.commands.close_run import CloseRunHandler
from nalar.application.features.runs.commands.open_lobby import JoinCodes, OpenLobbyHandler
from nalar.application.features.runs.commands.start_run import StartRunHandler
from nalar.application.features.runs.commands.warm_run import WarmRunHandler
from nalar.application.features.scheduler.commands.tick import SchedulerTiming, TickHandler
from nalar.application.features.sessions.commands.ingest_telemetry import IngestTelemetryHandler
from nalar.application.features.sessions.commands.join_run import JoinRunHandler
from nalar.application.features.sessions.commands.run_turn_step import RunTurnStepHandler
from nalar.application.features.sessions.commands.safety_action import SafetyActionHandler
from nalar.application.features.sessions.commands.start_window_session import (
    StartWindowSessionHandler,
)
from nalar.application.features.sessions.commands.submit_answer import SubmitAnswerHandler
from nalar.application.features.sessions.commands.submit_warmup import SubmitWarmupHandler
from nalar.application.features.sessions.queries.lobby_state import LobbyStateQuery
from nalar.application.features.sessions.queries.reflection import ReflectionQuery
from nalar.application.features.sessions.queries.session_state import SessionStateQuery
from nalar.application.features.sessions.queries.student_missions import StudentMissionsQuery
from nalar.application.features.sessions.timing import TurnTiming
from nalar.application.ports.ai import AiGateway
from nalar.application.ports.auth import (
    IdentityProvider,
    LoginAttempts,
    SessionRevocations,
    TokenVerifier,
)
from nalar.application.ports.background import BackgroundWork
from nalar.application.ports.clock import Clock
from nalar.application.ports.queue import QueueConsumer
from nalar.application.ports.readiness import ReadinessProbe
from nalar.application.ports.uow import UnitOfWork
from nalar.bootstrap.background import InProcessBackground
from nalar.bootstrap.settings import Settings
from nalar.domain.integrity import IntegrityConfig
from nalar.domain.placeholders import NarrativeLexicon
from nalar.infrastructure.ai.client import AiServiceClient, AiTimeouts
from nalar.infrastructure.auth.gotrue import SupabaseIdentityProvider
from nalar.infrastructure.auth.jwt import SupabaseJwtVerifier
from nalar.infrastructure.auth.redis_state import RedisAuthState
from nalar.infrastructure.clock import SystemClock
from nalar.infrastructure.config import load_integrity_config, load_narrative_lexicon
from nalar.infrastructure.db.pool import create_pool
from nalar.infrastructure.db.uow import PgUnitOfWork
from nalar.infrastructure.queue.pgmq import PgmqConsumer
from nalar.infrastructure.readiness import PoolAndAiProbe
from nalar.presentation.api.rate_limit import RateLimiter


class InfrastructureProvider(Provider):
    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._settings = settings

    @provide(scope=Scope.APP)
    def settings(self) -> Settings:
        return self._settings

    @provide(scope=Scope.APP)
    def clock(self) -> Clock:
        return SystemClock()

    @provide(scope=Scope.APP)
    def background(self, container: AsyncContainer) -> BackgroundWork:
        return InProcessBackground(container)

    @provide(scope=Scope.APP)
    def join_codes(self) -> JoinCodes:
        return JoinCodes()

    @provide(scope=Scope.APP)
    def join_limiter(self) -> RateLimiter:
        return RateLimiter(self._settings.join_rate_limit_per_minute)

    @provide(scope=Scope.APP)
    def turn_timing(self) -> TurnTiming:
        return TurnTiming(timedelta(seconds=self._settings.turn_recovery_after_s))

    @provide(scope=Scope.APP)
    def integrity_config(self) -> IntegrityConfig:
        return load_integrity_config()

    @provide(scope=Scope.APP)
    def narrative_lexicon(self) -> NarrativeLexicon:
        return load_narrative_lexicon()

    @provide(scope=Scope.APP)
    def scheduler_timing(self) -> SchedulerTiming:
        return SchedulerTiming(
            recovery_after=timedelta(seconds=self._settings.turn_recovery_after_s),
            evaluation_sweep_after=timedelta(seconds=self._settings.evaluation_sweep_after_s),
            finalize_cooldown=timedelta(seconds=self._settings.finalize_cooldown_s),
        )

    @provide(scope=Scope.APP)
    def publish_defaults(self) -> PublishDefaults:
        return PublishDefaults(planner_mode=self._settings.default_planner_mode)

    @provide(scope=Scope.APP)
    async def token_verifier(self) -> AsyncIterator[TokenVerifier]:
        secret = self._settings.supabase_jwt_secret
        async with httpx.AsyncClient() as http:
            yield SupabaseJwtVerifier(
                http,
                jwks_url=self._settings.jwks_url,
                audience=self._settings.jwt_audience,
                hs256_secret=secret.get_secret_value() if secret else None,
            )

    @provide(scope=Scope.APP)
    async def identity_provider(self) -> AsyncIterator[IdentityProvider]:
        s = self._settings
        async with httpx.AsyncClient(
            base_url=s.supabase_url,
            headers={"apikey": s.supabase_anon_key.get_secret_value()},
            timeout=s.auth_timeout_s,
        ) as http:
            yield SupabaseIdentityProvider(http)

    @provide(scope=Scope.APP)
    async def auth_state(self) -> AsyncIterator[RedisAuthState]:
        s = self._settings
        client = aioredis.from_url(
            s.redis_url.get_secret_value(),
            socket_timeout=s.redis_timeout_s,
            socket_connect_timeout=s.redis_timeout_s,
            health_check_interval=30,
        )
        yield RedisAuthState(client, s.access_token_ttl_s, s.login_rate_limit_per_minute)
        await client.aclose()

    revocations = alias(source=RedisAuthState, provides=SessionRevocations)
    login_attempts = alias(source=RedisAuthState, provides=LoginAttempts)

    @provide(scope=Scope.APP)
    async def pool(self) -> AsyncIterator[asyncpg.Pool]:
        pool = await create_pool(
            self._settings.database_url.get_secret_value(),
            min_size=self._settings.db_pool_min_size,
            max_size=self._settings.db_pool_max_size,
            statement_cache_size=self._settings.db_statement_cache_size,
            command_timeout=self._settings.db_command_timeout_s,
        )
        yield pool
        await pool.close()

    @provide(scope=Scope.APP)
    def readiness(self, container: AsyncContainer) -> ReadinessProbe:
        return PoolAndAiProbe(lambda: container.get(asyncpg.Pool), self._settings.ai_base_url)

    @provide(scope=Scope.REQUEST)
    def unit_of_work(self, pool: asyncpg.Pool) -> UnitOfWork:
        return PgUnitOfWork(pool.acquire)

    @provide(scope=Scope.APP)
    def queue_consumer(self, pool: asyncpg.Pool) -> QueueConsumer:
        return PgmqConsumer(pool)

    @provide(scope=Scope.APP)
    async def ai_gateway(self) -> AsyncIterator[AiGateway]:
        s = self._settings
        async with httpx.AsyncClient(
            base_url=s.ai_base_url,
            headers={"X-Service-Key": s.ai_service_key.get_secret_value()},
        ) as http:
            yield AiServiceClient(
                http,
                AiTimeouts(
                    turn_s=s.ai_turn_timeout_s,
                    warm_s=s.ai_warm_timeout_s,
                    evaluate_s=s.ai_evaluate_timeout_s,
                    embed_s=s.ai_embed_timeout_s,
                    s5_insight_s=s.ai_s5_insight_timeout_s,
                    s5_summary_s=s.ai_s5_summary_timeout_s,
                ),
            )


class ApplicationProvider(Provider):
    scope = Scope.REQUEST
    handlers = provide_all(
        MeQuery,
        PublishHandler,
        TeacherAssignmentsQuery,
        TeacherPublicationsQuery,
        OpenLobbyHandler,
        StartRunHandler,
        CloseRunHandler,
        WarmRunHandler,
        JoinRunHandler,
        SubmitWarmupHandler,
        StartWindowSessionHandler,
        LobbyStateQuery,
        StudentMissionsQuery,
        SubmitAnswerHandler,
        SessionStateQuery,
        RunTurnStepHandler,
        IngestTelemetryHandler,
        TickHandler,
        EvaluateSessionHandler,
        ReflectionQuery,
        MonitorQuery,
        SessionReportQuery,
        ClassMapQuery,
        SafetyActionHandler,
        OverrideScoreHandler,
        ReviewFlagHandler,
        ComputeSessionFlagsHandler,
        ComputePublicationSimilarityHandler,
        ReleasePreviewQuery,
        ReleaseHandler,
        FinalizePublicationHandler,
        ReleaseRemindersHandler,
        ChildrenQuery,
        ProgressQuery,
        ParentReflectionsQuery,
        PreferencesQuery,
        SetPreferencesHandler,
    )


def build_container(settings: Settings, *extra: Provider) -> AsyncContainer:
    return make_async_container(InfrastructureProvider(settings), ApplicationProvider(), *extra)
