from uuid import UUID

DIGEST_DELIVERY_KIND = "digest_delivery"


def digest_delivery_message(digest_id: UUID) -> dict[str, str]:
    return {"kind": DIGEST_DELIVERY_KIND, "notification_id": str(digest_id)}
