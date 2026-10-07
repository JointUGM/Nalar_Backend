import re


def normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _comparable(text: str) -> str:
    # A PDF line break after a hyphen (langkah- langkah) is layout, not content.
    return re.sub(r"-\s+", "-", normalized(text))


def verify_source(text: str, start: int, end: int, pages: dict[int, str]) -> None:
    if (
        start < 1
        or end < start
        or end > max(pages, default=0)
        or any(not pages.get(p, "").strip() for p in range(start, end + 1))
    ):
        raise ValueError("SOURCE_PAGE_INVALID")
    value = _comparable(text)
    source = _comparable(" ".join(pages[p] for p in range(start, end + 1)))
    if not value or value not in source:
        raise ValueError("SOURCE_TEXT_MISMATCH")
