import hashlib
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

from nalar.application.features.auth.commands.change_password import (
    ChangePasswordHandler,
    PasswordMutationHandler,
)
from nalar.application.features.auth.commands.confirm_password_reset import (
    ConfirmPasswordResetHandler,
)
from nalar.application.features.auth.commands.request_password_reset import (
    PasswordResetPolicy,
    RequestPasswordResetHandler,
)
from nalar.application.features.auth.commands.send_password_reset import SendPasswordResetHandler
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
from nalar.application.features.jobs.queries.get_job import GetJobQuery
from nalar.application.features.knowledge_base.commands.add_material import AddMaterialHandler
from nalar.application.features.knowledge_base.commands.build_section import (
    BuildSectionHandler,
    S1Settings,
)
from nalar.application.features.knowledge_base.commands.create_kb import (
    CreateKbHandler,
    UploadLimits,
)
from nalar.application.features.knowledge_base.commands.detect_sections import (
    DetectSectionsHandler,
)
from nalar.application.features.knowledge_base.commands.edit_item import EditItemHandler
from nalar.application.features.knowledge_base.commands.request_build import RequestBuildHandler
from nalar.application.features.knowledge_base.commands.review_item import ReviewItemHandler
from nalar.application.features.knowledge_base.queries.get_kb import GetKbQuery
from nalar.application.features.knowledge_base.queries.list_kbs import ListKbsQuery
from nalar.application.features.knowledge_base.queries.list_sections import ListSectionsQuery
from nalar.application.features.knowledge_base.queries.review_queue import ReviewQueueQuery
from nalar.application.features.missions.commands.create_mission import CreateMissionHandler
from nalar.application.features.missions.commands.create_version import CreateVersionHandler
from nalar.application.features.missions.commands.generate_mission import (
    BuildMissionHandler,
    GenerateMissionHandler,
)
from nalar.application.features.missions.commands.review_version import ReviewVersionHandler
from nalar.application.features.missions.queries.get_version import GetVersionQuery
from nalar.application.features.missions.queries.list_missions import ListMissionsQuery
from nalar.application.features.onboarding.commands.activate_account import (
    ActivateAccountHandler,
    ActivationPolicy,
)
from nalar.application.features.onboarding.commands.reconcile_login import (
    ReconcileOnboardingHandler,
)
from nalar.application.features.onboarding.commands.request_invitations import (
    InvitationRequestLimits,
    RequestInvitationsHandler,
)
from nalar.application.features.onboarding.commands.send_invitation import (
    InvitationTiming,
    SendAccountInvitationHandler,
)
from nalar.application.features.onboarding.queries.list_invitations import ListInvitationsQuery
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
from nalar.application.features.results.queries.class_students import ClassStudentsQuery
from nalar.application.features.results.queries.monitor import MonitorQuery
from nalar.application.features.results.queries.session_report import SessionReportQuery
from nalar.application.features.results.queries.teacher_attention import TeacherAttentionQuery
from nalar.application.features.roster.commands.import_roster import ImportRosterHandler
from nalar.application.features.roster.commands.upload_roster import (
    RosterLimits,
    UploadRosterHandler,
)
from nalar.application.features.roster.queries.get_import import GetImportQuery
from nalar.application.features.roster.queries.list_academic_years import ListAcademicYearsQuery
from nalar.application.features.runs.commands.close_run import CloseRunHandler
from nalar.application.features.runs.commands.open_lobby import JoinCodes, OpenLobbyHandler
from nalar.application.features.runs.commands.start_run import StartRunHandler
from nalar.application.features.runs.commands.warm_run import WarmRunHandler
from nalar.application.features.scheduler.commands.deliver_digest import DeliverDigestHandler
from nalar.application.features.scheduler.commands.tick import SchedulerTiming, TickHandler
from nalar.application.features.scheduler.commands.weekly_digest import (
    DigestTiming,
    WeeklyDigestHandler,
)
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
from nalar.application.features.sessions.queries.student_reflections import StudentReflectionsQuery
from nalar.application.features.sessions.timing import TurnTiming
from nalar.application.ports.ai import AiGateway
from nalar.application.ports.auth import (
    BrowserSessions,
    IdentityProvider,
    LoginAttempts,
    SessionRevocations,
    TokenVerifier,
)
from nalar.application.ports.auth_admin import AuthAdmin
from nalar.application.ports.background import BackgroundWork
from nalar.application.ports.clock import Clock
from nalar.application.ports.mailer import Mailer
from nalar.application.ports.password_resets import CredentialState
from nalar.application.ports.queue import QueueConsumer
from nalar.application.ports.readiness import ReadinessProbe
from nalar.application.ports.storage import ObjectStorage
from nalar.application.ports.uow import UnitOfWork
from nalar.bootstrap.background import InProcessBackground
from nalar.bootstrap.settings import Settings
from nalar.domain.integrity import IntegrityConfig
from nalar.domain.placeholders import NarrativeLexicon
from nalar.infrastructure.ai.client import AiServiceClient, AiTimeouts
from nalar.infrastructure.auth.admin import SupabaseAuthAdmin
from nalar.infrastructure.auth.credential_state import PgCredentialState
from nalar.infrastructure.auth.gotrue import SupabaseIdentityProvider
from nalar.infrastructure.auth.jwt import SupabaseJwtVerifier
from nalar.infrastructure.auth.redis_sessions import RedisBrowserSessions
from nalar.infrastructure.auth.redis_state import RedisAuthState
from nalar.infrastructure.clock import SystemClock
from nalar.infrastructure.config import load_integrity_config, load_narrative_lexicon
from nalar.infrastructure.db.pool import create_pool
from nalar.infrastructure.db.uow import PgUnitOfWork
from nalar.infrastructure.email.smtp import DisabledMailer, GmailSmtpMailer
from nalar.infrastructure.queue.pgmq import PgmqConsumer
from nalar.infrastructure.readiness import PoolAndAiProbe
from nalar.infrastructure.storage.supabase_storage import SupabaseStorage
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
    async def storage(self) -> AsyncIterator[ObjectStorage]:
        s = self._settings
        key = s.supabase_service_role_key.get_secret_value()
        async with httpx.AsyncClient(
            base_url=s.supabase_url,
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            timeout=s.storage_timeout_s,
        ) as http:
            yield SupabaseStorage(http)

    @provide(scope=Scope.APP)
    def roster_limits(self) -> RosterLimits:
        return RosterLimits(
            self._settings.roster_max_upload_bytes,
            self._settings.roster_stale_after_s,
            self._settings.student_login_domain,
            self._settings.account_email_queue_ttl_s,
        )

    @provide(scope=Scope.APP)
    async def auth_admin(self) -> AsyncIterator[AuthAdmin]:
        s = self._settings
        key = s.supabase_service_role_key.get_secret_value()
        async with httpx.AsyncClient(
            base_url=s.supabase_url,
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            timeout=s.auth_timeout_s,
        ) as http:
            yield SupabaseAuthAdmin(
                http,
                email_request_timeout_s=s.account_email_request_timeout_s,
                email_total_timeout_s=s.account_email_total_timeout_s,
            )

    @provide(scope=Scope.APP)
    def invitation_timing(self) -> InvitationTiming:
        s = self._settings
        return InvitationTiming(
            enabled=s.account_email_enabled,
            activation_url=s.account_email_activation_url,
            sender_key="supabase-auth:"
            + hashlib.sha256(s.supabase_url.rstrip("/").encode()).hexdigest(),
            total_timeout_s=s.account_email_total_timeout_s,
            lease=timedelta(seconds=s.account_email_lease_s),
            dispatch_ttl=timedelta(seconds=s.account_email_dispatch_ttl_s),
            link_lifetime=timedelta(seconds=s.account_email_link_lifetime_s),
            max_attempts=s.account_email_max_attempts,
            retry_delays=tuple(timedelta(seconds=v) for v in s.account_email_retry_delays_s),
            hourly_limit=s.account_email_hourly_limit,
            daily_limit=s.account_email_daily_limit,
            batch_size=s.account_email_batch_size,
            sender_spacing=timedelta(seconds=s.account_email_sender_spacing_s),
            auth_cooldown=timedelta(seconds=s.account_email_auth_cooldown_s),
        )

    @provide(scope=Scope.APP)
    def invitation_request_limits(self) -> InvitationRequestLimits:
        s = self._settings
        return InvitationRequestLimits(
            timedelta(seconds=s.account_email_queue_ttl_s),
            timedelta(seconds=s.account_email_resend_cooldown_s),
            s.account_email_lookup_timeout_s,
            s.account_email_lookup_concurrency,
        )

    @provide(scope=Scope.APP)
    def activation_policy(self) -> ActivationPolicy:
        s = self._settings
        return ActivationPolicy(
            s.auth_password_min_length,
            s.auth_password_required_characters,
            s.auth_activation_timeout_s,
            timedelta(seconds=s.auth_activation_lease_s),
            s.auth_timeout_s,
        )

    @provide(scope=Scope.APP)
    def password_reset_policy(self) -> PasswordResetPolicy:
        s = self._settings
        return PasswordResetPolicy(
            s.password_reset_enabled,
            s.password_reset_url,
            timedelta(seconds=s.account_email_queue_ttl_s),
            timedelta(seconds=s.account_email_resend_cooldown_s),
        )

    @provide(scope=Scope.APP)
    def credential_state(self, pool: asyncpg.Pool) -> CredentialState:
        return PgCredentialState(pool.acquire)

    @provide(scope=Scope.APP)
    def upload_limits(self) -> UploadLimits:
        return UploadLimits(self._settings.kb_max_upload_bytes)

    @provide(scope=Scope.APP)
    def s1_settings(self) -> S1Settings:
        s = self._settings
        return S1Settings(s.embedding_model, s.default_phase, s.kb_build_stale_after_s)

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
    def digest_timing(self) -> DigestTiming:
        s = self._settings
        return DigestTiming(
            lease=timedelta(seconds=s.digest_lease_s),
            retry_window=timedelta(seconds=s.digest_retry_window_s),
            max_attempts=s.digest_max_attempts,
            dispatch_ttl=timedelta(seconds=s.digest_dispatch_ttl_s),
            retry_delays=tuple(timedelta(seconds=v) for v in s.digest_retry_delays_s),
            daily_limit=s.digest_daily_limit,
            batch_size=s.digest_batch_size,
            sender_spacing=timedelta(seconds=s.digest_sender_spacing_s),
            quota_cooldown=timedelta(seconds=s.digest_quota_cooldown_s),
            auth_cooldown=timedelta(seconds=s.digest_auth_cooldown_s),
        )

    @provide(scope=Scope.APP)
    def mailer(self) -> Mailer:
        s = self._settings
        if not s.email_enabled:
            return DisabledMailer()
        assert s.smtp_username and s.smtp_app_password
        return GmailSmtpMailer(
            s.smtp_username,
            s.smtp_app_password.get_secret_value(),
            s.email_from_name,
            command_timeout_s=s.smtp_command_timeout_s,
            data_timeout_s=s.smtp_data_timeout_s,
            total_timeout_s=s.email_timeout_s,
        )

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
    async def browser_sessions(
        self, identity: IdentityProvider, credentials: CredentialState
    ) -> AsyncIterator[BrowserSessions]:
        s = self._settings
        client = aioredis.from_url(
            s.redis_url.get_secret_value(),
            socket_timeout=s.redis_timeout_s,
            socket_connect_timeout=s.redis_timeout_s,
        )
        try:
            yield RedisBrowserSessions(
                client,
                identity,
                s.session_lifetime_s,
                s.session_refresh_margin_s,
                s.session_refresh_lock_s,
                credentials=credentials,
            )
        finally:
            await client.aclose()

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
                    s1_step_s=s.ai_s1_step_timeout_s,
                ),
            )


class ApplicationProvider(Provider):
    scope = Scope.REQUEST
    handlers = provide_all(
        UploadRosterHandler,
        ImportRosterHandler,
        GetImportQuery,
        ListAcademicYearsQuery,
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
        StudentReflectionsQuery,
        SubmitAnswerHandler,
        SessionStateQuery,
        RunTurnStepHandler,
        IngestTelemetryHandler,
        TickHandler,
        WeeklyDigestHandler,
        DeliverDigestHandler,
        SendAccountInvitationHandler,
        RequestInvitationsHandler,
        ListInvitationsQuery,
        ActivateAccountHandler,
        ReconcileOnboardingHandler,
        RequestPasswordResetHandler,
        SendPasswordResetHandler,
        ConfirmPasswordResetHandler,
        ChangePasswordHandler,
        PasswordMutationHandler,
        EvaluateSessionHandler,
        ReflectionQuery,
        MonitorQuery,
        SessionReportQuery,
        ClassMapQuery,
        ClassStudentsQuery,
        TeacherAttentionQuery,
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
        CreateMissionHandler,
        GenerateMissionHandler,
        BuildMissionHandler,
        CreateVersionHandler,
        ReviewVersionHandler,
        ListMissionsQuery,
        GetVersionQuery,
        CreateKbHandler,
        AddMaterialHandler,
        DetectSectionsHandler,
        BuildSectionHandler,
        RequestBuildHandler,
        ListKbsQuery,
        GetKbQuery,
        ListSectionsQuery,
        ReviewItemHandler,
        EditItemHandler,
        ReviewQueueQuery,
        GetJobQuery,
    )


def build_container(settings: Settings, *extra: Provider) -> AsyncContainer:
    return make_async_container(InfrastructureProvider(settings), ApplicationProvider(), *extra)
