import pytest
from pydantic import SecretStr, ValidationError

from nalar.bootstrap.settings import Settings


def test_production_refuses_to_start_without_its_secrets() -> None:
    with pytest.raises(ValidationError, match="NALAR_AI_SERVICE_KEY"):
        Settings(env="prod", _env_file=None)


def test_production_starts_with_its_secrets() -> None:
    settings = Settings(
        env="prod",
        _env_file=None,
        database_url=SecretStr(
            "postgresql://u:p@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres"
        ),
        ai_service_key=SecretStr("k"),
        supabase_service_role_key=SecretStr("s"),
    )
    assert settings.env == "prod"
