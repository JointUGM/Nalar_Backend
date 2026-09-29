import httpx

from nalar.bootstrap.app import create_app
from nalar.bootstrap.settings import Settings


async def test_ready_reports_the_database_and_tolerates_a_down_ai() -> None:
    app = create_app(Settings(ai_base_url="http://127.0.0.1:9"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"database": True, "ai": False}
