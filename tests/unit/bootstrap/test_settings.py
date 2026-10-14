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
        supabase_anon_key=SecretStr("a"),
        redis_url=SecretStr("rediss://default:p@example.upstash.io:6379"),
    )
    assert settings.env == "prod"


@pytest.mark.parametrize(
    "url",
    [
        "https://other.test/activate",
        "https://nalar.test/activate?redirect=other",
        "https://nalar.test/activate#",
        "https://nalar.test/activate?",
    ],
)
def test_account_activation_rejects_other_origins_and_preexisting_url_parameters(url: str) -> None:
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,
            env="test",
            account_email_enabled=True,
            account_email_activation_url=url,
            cors_origins=["https://nalar.test"],
        )
