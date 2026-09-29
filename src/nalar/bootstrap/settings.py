from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NALAR_", env_file=".env", extra="ignore")

    env: Literal["local", "test", "staging", "prod"] = "local"

    database_url: SecretStr = SecretStr("postgresql://postgres:postgres@127.0.0.1:54322/postgres")
    db_pool_min_size: int = 1
    db_pool_max_size: int = 20
    db_statement_cache_size: int = 100

    supabase_url: str = "http://127.0.0.1:54321"
    supabase_service_role_key: SecretStr = SecretStr("")
    supabase_anon_key: SecretStr = SecretStr("")
    seed_password: SecretStr = SecretStr("")
    supabase_jwt_secret: SecretStr | None = None
    jwt_audience: str = "authenticated"

    ai_base_url: str = "http://127.0.0.1:8000"
    ai_service_key: SecretStr = SecretStr("")
    ai_turn_timeout_s: float = 6.0
    ai_warm_timeout_s: float = 10.0
    ai_evaluate_timeout_s: float = 330.0
    ai_embed_timeout_s: float = 30.0
    ai_s1_step_timeout_s: float = 180.0

    kb_concurrency: int = 2
    eval_concurrency: int = 16
    default_concurrency: int = 4

    default_planner_mode: Literal["table", "hybrid"] = "table"
    join_rate_limit_per_minute: int = 10
    turn_recovery_after_s: float = 15.0
    evaluation_sweep_after_s: float = 120.0
    embedding_model: str = "text-embedding-3-small"
    cors_origins: list[str] = ["http://localhost:3000"]

    resend_api_key: SecretStr | None = None
    email_from: str | None = None

    @property
    def jwks_url(self) -> str:
        return f"{self.supabase_url}/auth/v1/.well-known/jwks.json"
