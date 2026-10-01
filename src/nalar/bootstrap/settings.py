from typing import Literal, Self

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NALAR_", env_file=".env", extra="ignore")

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
    redis_url: SecretStr = SecretStr("redis://127.0.0.1:6379")
    redis_timeout_s: float = 0.25

    ai_base_url: str = "http://127.0.0.1:8000"
    ai_service_key: SecretStr = SecretStr("")
    ai_turn_timeout_s: float = 6.0
    ai_warm_timeout_s: float = 10.0
    ai_evaluate_timeout_s: float = 330.0
    ai_embed_timeout_s: float = 30.0
    ai_s1_step_timeout_s: float = 180.0
    storage_timeout_s: float = 60.0
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
    cors_origins: list[str] = ["http://localhost:3000"]

    resend_api_key: SecretStr | None = None
    email_from: str | None = None

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
