from typing import Any

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
    "overrides",
    [
        {},
        {"smtp_username": "custom@school.test", "smtp_app_password": SecretStr("abcdefghijklmnop")},
        {"smtp_username": "nalar@gmail.com", "smtp_app_password": SecretStr("short")},
        {
            "smtp_username": "nalar@gmail.com",
            "smtp_app_password": SecretStr("abcdefghijklmnop"),
            "email_timeout_s": 180,
        },
    ],
)
def test_enabled_email_rejects_unsafe_configuration(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, email_enabled=True, **overrides)


def test_gmail_password_display_spaces_are_removed_and_errors_hide_input() -> None:
    settings = Settings(
        _env_file=None,
        email_enabled=True,
        smtp_username="NALAR@gmail.com",
        smtp_app_password=SecretStr("abcd efgh ijkl mnop"),
    )
    assert settings.smtp_username == "nalar@gmail.com"
    assert settings.smtp_app_password is not None
    assert settings.smtp_app_password.get_secret_value() == "abcdefghijklmnop"
    with pytest.raises(ValidationError) as caught:
        Settings(
            _env_file=None,
            email_enabled=True,
            smtp_username="nalar@gmail.com",
            smtp_app_password="sensitive-secret",
        )
    assert "sensitive-secret" not in str(caught.value)


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
