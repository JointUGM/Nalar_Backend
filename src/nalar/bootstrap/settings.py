import math
import re
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NALAR_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    env: Literal["local", "test", "staging", "prod"] = "local"

    database_url: SecretStr = SecretStr("postgresql://postgres:postgres@127.0.0.1:54322/postgres")
    db_pool_min_size: int = 1
    db_pool_max_size: int = 20
    db_statement_cache_size: int = 100
    db_command_timeout_s: float = 10.0

    supabase_url: str = "http://127.0.0.1:54321"
    supabase_service_role_key: SecretStr = SecretStr("")
    supabase_anon_key: SecretStr = SecretStr("")
    seed_password: SecretStr = SecretStr("")
    supabase_jwt_secret: SecretStr | None = None
    jwt_audience: str = "authenticated"
    auth_timeout_s: float = 5.0
    access_token_ttl_s: int = 3600
    login_rate_limit_per_minute: int = 10
    auth_password_min_length: int = Field(default=8, ge=8, le=256)
    auth_password_required_characters: tuple[
        Literal["lowercase", "uppercase", "digit", "symbol"], ...
    ] = ()
    auth_activation_timeout_s: float = Field(default=15, gt=0, allow_inf_nan=False)
    auth_activation_lease_s: float = Field(default=60, gt=0, allow_inf_nan=False)
    redis_url: SecretStr = SecretStr("redis://127.0.0.1:6379")
    redis_timeout_s: float = 0.25
    session_lifetime_s: int = 43200
    session_refresh_margin_s: int = 60
    session_refresh_lock_s: int = 15

    ai_base_url: str = "http://127.0.0.1:8000"
    ai_service_key: SecretStr = SecretStr("")
    ai_turn_timeout_s: float = 6.0
    ai_warm_timeout_s: float = 10.0
    ai_evaluate_timeout_s: float = 330.0
    ai_embed_timeout_s: float = 30.0
    ai_s1_step_timeout_s: float = 180.0
    storage_timeout_s: float = 60.0
    roster_max_upload_bytes: int = 5 * 1024 * 1024
    roster_stale_after_s: float = 120.0
    student_login_domain: str = "siswa.nalar.id"
    account_email_queue_ttl_s: float = Field(default=172800, gt=0, allow_inf_nan=False)
    account_email_enabled: bool = False
    account_email_activation_url: str | None = None
    account_email_request_timeout_s: float = Field(default=10, gt=0, allow_inf_nan=False)
    account_email_total_timeout_s: float = Field(default=15, gt=0, allow_inf_nan=False)
    account_email_lease_s: float = Field(default=60, gt=0, allow_inf_nan=False)
    account_email_dispatch_ttl_s: float = Field(default=1080, gt=0, allow_inf_nan=False)
    account_email_link_lifetime_s: float = Field(default=3600, gt=0, allow_inf_nan=False)
    account_email_max_attempts: int = Field(default=3, ge=1, le=3)
    account_email_retry_delays_s: tuple[int, ...] = (1800, 7200)
    account_email_hourly_limit: int = Field(default=20, ge=1, le=20)
    account_email_daily_limit: int = Field(default=200, ge=1, le=200)
    account_email_batch_size: int = Field(default=32, ge=1, le=100)
    account_email_sender_spacing_s: float = Field(default=1, ge=0, allow_inf_nan=False)
    account_email_auth_cooldown_s: float = Field(default=1800, gt=0, allow_inf_nan=False)
    account_email_resend_cooldown_s: float = Field(default=60, ge=60, allow_inf_nan=False)
    account_email_lookup_timeout_s: float = Field(default=30, gt=0, allow_inf_nan=False)
    account_email_lookup_concurrency: int = Field(default=4, ge=1, le=8)
    kb_max_upload_bytes: int = 50 * 1024 * 1024
    ai_s5_insight_timeout_s: float = 190.0
    ai_s5_summary_timeout_s: float = 90.0

    kb_concurrency: int = 2
    eval_concurrency: int = 16
    default_concurrency: int = 4

    default_planner_mode: Literal["table", "hybrid"] = "table"
    join_rate_limit_per_minute: int = 10
    turn_recovery_after_s: float = 15.0
    finalize_cooldown_s: float = 600.0
    evaluation_sweep_after_s: float = 120.0
    embedding_model: str = "text-embedding-3-small"
    default_phase: str = "D"
    kb_build_stale_after_s: float = 3600.0
    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:5173"]

    @property
    def session_cookie_name(self) -> str:
        return "__Host-nalar_session" if self.env in ("staging", "prod") else "nalar_session"

    @model_validator(mode="after")
    def account_delivery_limits(self) -> Self:
        if not self.account_email_enabled:
            return self
        if not (
            self.account_email_request_timeout_s
            < self.account_email_total_timeout_s
            < self.account_email_lease_s
            < self.account_email_dispatch_ttl_s
            < self.account_email_queue_ttl_s
        ):
            raise ValueError("invalid account-email timeout or lease ordering")
        if len(self.account_email_retry_delays_s) < self.account_email_max_attempts - 1 or any(
            delay <= 0 for delay in self.account_email_retry_delays_s
        ):
            raise ValueError("invalid account-email retry delays")
        url = urlsplit(self.account_email_activation_url or "")
        origin = f"{url.scheme}://{url.netloc}"
        if (
            url.path != "/activate"
            or self.account_email_activation_url != origin + "/activate"
            or url.query
            or url.fragment
            or url.username is not None
            or url.password is not None
            or not url.hostname
            or origin not in self.cors_origins
        ):
            raise ValueError("account activation URL must use a configured frontend origin")
        if url.scheme != "https" and not (
            self.env in ("local", "test")
            and url.scheme == "http"
            and url.hostname in ("localhost", "127.0.0.1", "::1")
        ):
            raise ValueError("account activation URL requires HTTPS")
        return self

    @model_validator(mode="after")
    def session_limits(self) -> Self:
        if (
            self.auth_activation_lease_s
            <= self.auth_activation_timeout_s + 2 * self.db_command_timeout_s
        ):
            raise ValueError("activation lease must cover Auth and database deadlines")
        if self.session_lifetime_s < 60 or self.session_refresh_margin_s < 0:
            raise ValueError("invalid session lifetime or refresh margin")
        if self.session_refresh_lock_s <= self.auth_timeout_s:
            raise ValueError("session refresh lock must exceed the auth timeout")
        if "*" in self.cors_origins:
            raise ValueError("cookie authentication requires explicit CORS origins")
        return self

    email_enabled: bool = False
    smtp_username: str | None = None
    smtp_app_password: SecretStr | None = None
    email_from_name: str = "NALAR"
    smtp_command_timeout_s: float = 10.0
    smtp_data_timeout_s: float = 60.0
    email_timeout_s: float = 90.0
    digest_lease_s: float = 180.0
    digest_dispatch_ttl_s: float = 1080.0
    digest_retry_window_s: float = 172800.0
    digest_max_attempts: int = 5
    digest_retry_delays_s: tuple[int, ...] = (1800, 7200, 21600, 43200)
    digest_daily_limit: int = 200
    digest_batch_size: int = 32
    digest_sender_spacing_s: float = 1.0
    digest_quota_cooldown_s: float = 86400.0
    digest_auth_cooldown_s: float = 1800.0

    @model_validator(mode="after")
    def digest_delivery_limits(self) -> Self:
        if not self.email_enabled:
            return self
        durations = (
            self.smtp_command_timeout_s,
            self.smtp_data_timeout_s,
            self.email_timeout_s,
            self.digest_lease_s,
            self.digest_dispatch_ttl_s,
            self.digest_retry_window_s,
            self.digest_sender_spacing_s,
            self.digest_quota_cooldown_s,
            self.digest_auth_cooldown_s,
            *self.digest_retry_delays_s,
        )
        if any(not math.isfinite(v) or v <= 0 for v in durations):
            raise ValueError("email durations must be finite and positive")
        if not (
            max(self.smtp_command_timeout_s, self.smtp_data_timeout_s)
            < self.email_timeout_s
            < self.digest_lease_s
            < self.digest_dispatch_ttl_s
        ):
            raise ValueError("email timeout, lease and dispatch reservation must increase")
        if not 1 <= self.digest_daily_limit <= 500 or not 1 <= self.digest_batch_size <= 200:
            raise ValueError("invalid digest daily or batch limit")
        if (
            not 1 <= self.digest_max_attempts <= 5
            or len(self.digest_retry_delays_s) < self.digest_max_attempts - 1
        ):
            raise ValueError("invalid digest attempt limit or retry delays")
        if not self.smtp_username or not self.smtp_app_password:
            raise ValueError(
                "enabled email requires NALAR_SMTP_USERNAME and NALAR_SMTP_APP_PASSWORD"
            )
        self.smtp_username = self.smtp_username.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9.]*@gmail\.com", self.smtp_username):
            raise ValueError("SMTP sender must be one personal Gmail address")
        password = self.smtp_app_password.get_secret_value().replace(" ", "")
        if not re.fullmatch(r"[A-Za-z0-9]{16}", password):
            raise ValueError("SMTP app password must contain sixteen letters or digits")
        if any(c in self.email_from_name for c in "\r\n"):
            raise ValueError("invalid email sender name")
        self.smtp_app_password = SecretStr(password)
        return self

    @model_validator(mode="after")
    def deployed_needs_secrets(self) -> Self:
        if self.env not in ("staging", "prod"):
            return self
        missing = [
            name
            for name, value in (
                ("NALAR_AI_SERVICE_KEY", self.ai_service_key),
                ("NALAR_SUPABASE_SERVICE_ROLE_KEY", self.supabase_service_role_key),
                ("NALAR_SUPABASE_ANON_KEY", self.supabase_anon_key),
            )
            if not value.get_secret_value()
        ]
        url = self.database_url.get_secret_value()
        if "127.0.0.1" in url or "localhost" in url:
            missing.append("NALAR_DATABASE_URL")
        redis_url = self.redis_url.get_secret_value()
        if "127.0.0.1" in redis_url or "localhost" in redis_url:
            missing.append("NALAR_REDIS_URL")
        if missing:
            raise ValueError(f"missing settings for {self.env}: {', '.join(missing)}")
        return self

    @property
    def jwks_url(self) -> str:
        return f"{self.supabase_url}/auth/v1/.well-known/jwks.json"
