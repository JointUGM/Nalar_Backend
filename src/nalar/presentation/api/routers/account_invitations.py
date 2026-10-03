from dataclasses import asdict
from typing import Annotated
from uuid import UUID

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Query

from nalar.application.features.onboarding.commands.request_invitations import (
    RequestInvitationsHandler,
)
from nalar.application.features.onboarding.queries.list_invitations import ListInvitationsQuery
from nalar.application.ports.activations import InvitationState
from nalar.presentation.api.deps import CurrentUser
from nalar.presentation.api.schemas.account_invitations import (
    InvitationAdmissionOut,
    InvitationPageOut,
    InvitationRequestIn,
    InvitationsQueuedOut,
    InvitationStatusOut,
)

router = APIRouter(tags=["admin"], route_class=DishkaRoute)


@router.post(
    "/schools/{school_id}/account-invitations", status_code=202, response_model=InvitationsQueuedOut
)
async def request_invitations(
    school_id: UUID,
    body: InvitationRequestIn,
    user: CurrentUser,
    handler: FromDishka[RequestInvitationsHandler],
) -> InvitationsQueuedOut:
    admissions = await handler.execute(user.id, school_id, body.user_ids, body.resend)
    queued = sum(item.queued for item in admissions)
    return InvitationsQueuedOut(
        queued=queued,
        skipped=len(admissions) - queued,
        notification_ids=[item.notification_id for item in admissions if item.notification_id],
        items=[InvitationAdmissionOut(**asdict(item)) for item in admissions],
    )


@router.get("/schools/{school_id}/account-invitations", response_model=InvitationPageOut)
async def list_invitations(
    school_id: UUID,
    user: CurrentUser,
    query: FromDishka[ListInvitationsQuery],
    cursor: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    state: InvitationState | None = None,
) -> InvitationPageOut:
    page = await query.execute(user.id, school_id, cursor, limit, state)
    return InvitationPageOut(
        items=[InvitationStatusOut(**asdict(item)) for item in page.items],
        counts=page.counts,
        total=page.total,
        next_cursor=page.next_cursor,
    )
