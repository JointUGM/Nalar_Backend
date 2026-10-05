from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

type ReferenceRow = dict[str, Any]


@dataclass(frozen=True)
class ReferencePolicy:
    max_bytes: int
    max_pages: int
    max_characters: int
    max_statements: int
    lease_s: int
    max_attempts: int
    embedding_model: str


class ReferencePdf(Protocol):
    """Extracts numbered source pages and copies selected pages without model generation."""

    async def extract(self, data: bytes) -> dict[int, str]: ...

    async def select(self, data: bytes, pages: list[int]) -> bytes: ...


class NationalReferencesRepo(Protocol):
    """Platform references, reviewed CP publication and school-scoped source adoption."""

    async def insert(self, values: ReferenceRow) -> None: ...

    async def get(self, document_id: UUID, *, lock: bool = False) -> ReferenceRow | None: ...

    async def documents(self, *, published_only: bool) -> list[ReferenceRow]: ...

    async def queue(self, document_id: UUID, job_id: UUID, status: str) -> None: ...

    async def claim(self, document_id: UUID, job_id: UUID, token: UUID, lease_s: int) -> bool: ...

    async def pages(self, document_id: UUID) -> dict[int, str]: ...

    async def extracted(self, document_id: UUID, token: UUID, pages: dict[int, str]) -> bool: ...

    async def review(self, document_id: UUID, revision: int, draft: ReferenceRow) -> int: ...

    async def publish(
        self,
        document_id: UUID,
        token: UUID,
        actor_id: UUID,
        vectors: list[list[float]],
        model: str,
    ) -> bool: ...

    async def release(self, document_id: UUID, token: UUID, error_code: str | None) -> bool: ...

    async def adoption(self, kb_id: UUID, document_id: UUID) -> ReferenceRow | None: ...

    async def attach(self, material_id: UUID, document_id: UUID, pages: list[int]) -> None: ...
