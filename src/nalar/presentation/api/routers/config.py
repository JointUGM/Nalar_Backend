from fastapi import APIRouter, Request, Response

from nalar.presentation.api.schemas.config import ConfigOut

router = APIRouter(tags=["config"])


@router.get("/config", response_model=ConfigOut)
async def config(request: Request, response: Response) -> ConfigOut:
    response.headers["Cache-Control"] = "no-store"
    return ConfigOut.model_validate(request.app.state.public_config)
