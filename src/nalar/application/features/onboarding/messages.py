from uuid import UUID

ACCOUNT_INVITATION_KIND = "account_invitation"


def invitation_message(notification_id: UUID) -> dict[str, str]:
    return {"kind": ACCOUNT_INVITATION_KIND, "notification_id": str(notification_id)}
