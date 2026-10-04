from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter

from nalar.application.features.identity.queries.me import MeQuery
from nalar.presentation.api.deps import CurrentUser
from nalar.presentation.api.schemas.teacher import MeOut, RoleOut

router = APIRouter(tags=["identity"], route_class=DishkaRoute)


@router.get("/me", response_model=MeOut)
async def me(user: CurrentUser, query: FromDishka[MeQuery]) -> MeOut:
    result = await query.execute(user.id)
    return MeOut(
        user_id=result.user_id,
        email=result.email,
        full_name=result.full_name,
        roles=[
            RoleOut(role=r.role, school_id=r.school_id, school_name=r.school_name)
            for r in result.roles
        ],
        is_parent=result.is_parent,
        is_platform_admin=result.is_platform_admin,
    )
