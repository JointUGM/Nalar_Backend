import re
from collections import Counter

_LAYOUT_LINE_MAX = 160
_SENTENCE_END = (".", "!", "?", ";", ":", ",")


def normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _comparable(text: str) -> str:
    # A PDF line break after a hyphen (langkah- langkah) is layout, not content.
    return re.sub(r"-\s+", "-", normalized(text))


def _layout_lines(pages: dict[int, str]) -> set[str]:
    """Short non-sentence lines that are the first or last line of two or more pages, such as a
    table header repeated on a continued page ("Elemen Fase D") that extraction kept because it
    tops too few pages. A repeated line inside a page is content (a sub-heading)."""
    counts = Counter(
        key
        for text in pages.values()
        if (lines := [k for line in text.splitlines() if (k := normalized(line).casefold())])
        for key in {lines[0], lines[-1]}
    )
    return {
        key
        for key, seen in counts.items()
        if seen >= 2 and 0 < len(key) <= _LAYOUT_LINE_MAX and not key.endswith(_SENTENCE_END)
    }


def _joined_without_layout(start: int, end: int, pages: dict[int, str]) -> str:
    """Join the cited pages, dropping layout lines only where one page meets the next."""
    layout = _layout_lines(pages)

    def skippable(line: str) -> bool:
        key = normalized(line).casefold()
        return not key or key in layout

    parts = []
    for page in range(start, end + 1):
        lines = pages[page].splitlines()
        low, high = 0, len(lines)
        while page > start and low < high and skippable(lines[low]):
            low += 1
        while page < end and high > low and skippable(lines[high - 1]):
            high -= 1
        parts.append("\n".join(lines[low:high]))
    return " ".join(parts)


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
    if value and value in source:
        return
    # ponytail: scans the whole document per failing multi-page quote; cache per document if a
    # draft with hundreds of cross-page quotes gets slow.
    if (
        not value
        or start == end
        or value not in _comparable(_joined_without_layout(start, end, pages))
    ):
        raise ValueError("SOURCE_TEXT_MISMATCH")
