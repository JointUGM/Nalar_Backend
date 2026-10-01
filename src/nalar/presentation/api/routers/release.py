from dataclasses import asdict
from uuid import UUID

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter

from nalar.application.features.release.commands.release import Release, ReleaseHandler
from nalar.application.features.release.queries.preview import ReleasePreviewQuery
from nalar.presentation.api.deps import CurrentUser
from nalar.presentation.api.schemas.release import (
    BlockerOut,
    ReleasedOut,
    ReleaseIn,
    ReleasePreviewOut,
    SummaryPreviewOut,
)

router = APIRouter(tags=["release"], route_class=DishkaRoute)


@router.get("/publications/{publication_id}/release-preview", response_model=ReleasePreviewOut)
async def preview(
    publication_id: UUID, user: CurrentUser, query: FromDishka[ReleasePreviewQuery]
) -> ReleasePreviewOut:
    p = await query.execute(user.id, publication_id)
    return ReleasePreviewOut(
        ready=p.ready,
        blockers=[BlockerOut(code=c, count=n) for c, n in p.blockers],
        eligible_count=p.eligible_count,
        ineligible_count=p.ineligible_count,
        summaries=[SummaryPreviewOut(**asdict(s)) for s in p.summaries],
        released_at=p.released_at,
    )


@router.post("/publications/{publication_id}/release", response_model=ReleasedOut)
async def release(
    publication_id: UUID, body: ReleaseIn, user: CurrentUser, handler: FromDishka[ReleaseHandler]
) -> ReleasedOut:
    done = await handler.execute(Release(user.id, publication_id, body.expected_eligible_count))
    return ReleasedOut(**asdict(done))
