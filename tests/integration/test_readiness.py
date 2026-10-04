import httpx
from pydantic import SecretStr

from nalar.bootstrap.app import create_app
from nalar.bootstrap.settings import Settings
from tests.integration.conftest import TEST_DATABASE_URL


async def test_ready_reports_the_database_and_tolerates_a_down_ai() -> None:
    app = create_app(
        Settings(
            env="test",
            _env_file=None,
            database_url=SecretStr(TEST_DATABASE_URL),
            ai_base_url="http://127.0.0.1:9",
        )
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"database": True, "ai": False}


async def test_an_unreachable_database_is_503_not_an_error() -> None:
    settings = Settings(
        env="test",
        _env_file=None,
        database_url=SecretStr("postgresql://postgres:postgres@127.0.0.1:1/postgres"),
        ai_base_url="http://127.0.0.1:9",
    )
    transport = httpx.ASGITransport(app=create_app(settings))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"database": False, "ai": False}
