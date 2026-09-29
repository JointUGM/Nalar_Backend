import httpx


class SupabaseStorage:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def ensure_bucket(self, name: str) -> None:
        response = await self._http.post(
            "/storage/v1/bucket", json={"id": name, "name": name, "public": False}
        )
        if response.status_code in (200, 201):
            return
        text = response.text.lower()
        if response.status_code == 409 or "duplicate" in text or "already exists" in text:
            return
        response.raise_for_status()
