from typing import Protocol

MATERIALS_BUCKET = "teaching-materials"
ROSTER_BUCKET = "roster-imports"


class ObjectStorage(Protocol):
    async def upload(self, bucket: str, path: str, data: bytes, content_type: str) -> None:
        """Overwrites an existing object, so a retried upload is harmless."""
        ...

    async def download(self, bucket: str, path: str) -> bytes: ...
