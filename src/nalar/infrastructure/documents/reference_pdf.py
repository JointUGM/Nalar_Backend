import asyncio
from io import BytesIO

from pypdf import PdfReader, PdfWriter

from nalar.application.ports.national_references import ReferencePolicy


class NationalReferencePdf:
    def __init__(self, policy: ReferencePolicy) -> None:
        self._policy = policy

    def _reader(self, data: bytes) -> PdfReader:
        if len(data) > self._policy.max_bytes or not data.startswith(b"%PDF-"):
            raise ValueError("REFERENCE_PDF_INVALID")
        reader = PdfReader(BytesIO(data), strict=True)
        if reader.is_encrypted:
            raise ValueError("REFERENCE_PDF_ENCRYPTED")
        if not 1 <= len(reader.pages) <= self._policy.max_pages:
            raise ValueError("REFERENCE_PAGE_LIMIT")
        return reader

    async def extract(self, data: bytes) -> dict[int, str]:
        return await asyncio.to_thread(self._extract, data)

    def _extract(self, data: bytes) -> dict[int, str]:
        reader = self._reader(data)
        pages: dict[int, str] = {}
        characters = 0
        for number, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ""
            characters += len(text)
            if characters > self._policy.max_characters:
                raise ValueError("REFERENCE_TEXT_LIMIT")
            pages[number] = text
        if not any(text.strip() for text in pages.values()):
            raise ValueError("REFERENCE_OCR_REQUIRED")
        return pages

    async def select(self, data: bytes, pages: list[int]) -> bytes:
        return await asyncio.to_thread(self._select, data, pages)

    def _select(self, data: bytes, pages: list[int]) -> bytes:
        reader = self._reader(data)
        writer = PdfWriter()
        for number in pages:
            if number < 1 or number > len(reader.pages):
                raise ValueError("SOURCE_PAGE_INVALID")
            writer.add_page(reader.pages[number - 1])
        output = BytesIO()
        writer.write(output)
        return output.getvalue()
