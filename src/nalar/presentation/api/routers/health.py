from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from nalar.application.ports.readiness import ReadinessProbe

router = APIRouter(tags=["health"], route_class=DishkaRoute)


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(probe: FromDishka[ReadinessProbe]) -> JSONResponse:
    result = await probe.check()
    # NFR-R4: a down AI service still leaves the API serving fallback questions, so only the
    # database decides readiness.
    return JSONResponse(
        {"database": result.database, "ai": result.ai},
        status_code=200 if result.database else 503,
    )
