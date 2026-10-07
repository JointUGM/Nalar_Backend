import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date
from math import ceil
from typing import Any

from nalar.domain.national_references import normalized, verify_source

_MONTHS = {
    "januari": 1,
    "februari": 2,
    "maret": 3,
    "april": 4,
    "mei": 5,
    "juni": 6,
    "juli": 7,
    "agustus": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "desember": 12,
}
_DATE = re.compile(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})")
_NUMBER = re.compile(r"\d+")
_MARKERS = ("capaian pembelajaran", "elemen", "pada akhir fase", "peserta didik")
_RUNNING_LINE_MAX = 160
_RUNNING_MIN_PAGES = 3


def _line_key(line: str) -> str:
    return _NUMBER.sub("#", normalized(line)).casefold()


def _is_running_candidate(line: str) -> bool:
    value = normalized(line)
    return 0 < len(value) <= _RUNNING_LINE_MAX and not value.isdigit()


def _edge(lines: Sequence[str], last: bool) -> int | None:
    filled = [i for i, line in enumerate(lines) if normalized(line)]
    if not filled:
        return None
    return filled[-1] if last else filled[0]


def strip_running_lines(pages: Mapping[int, str], share: float) -> dict[int, str]:
    """Drop running headers, footers and page numbers so a quote can cross a page break.

    A bare number is a page number only when it sits on the same edge of enough pages;
    a number inside a sentence is content.
    """
    threshold = max(_RUNNING_MIN_PAGES, ceil(len(pages) * share))
    counts = Counter(
        key
        for text in pages.values()
        for key in {_line_key(line) for line in text.splitlines() if _is_running_candidate(line)}
    )
    running = {key for key, seen in counts.items() if seen >= threshold}
    kept = {
        number: [
            line
            for line in text.splitlines()
            if not (_is_running_candidate(line) and _line_key(line) in running)
        ]
        for number, text in pages.items()
    }

    def numbered_at(last: bool) -> set[int]:
        found = set()
        for number, lines in kept.items():
            index = _edge(lines, last)
            if index is not None and normalized(lines[index]).isdigit():
                found.add(number)
        return found

    owners = {last: numbered_at(last) for last in (False, True)}
    result = {}
    for number, lines in kept.items():
        drop = {
            _edge(lines, last)
            for last, with_number in owners.items()
            if len(with_number) >= threshold and number in with_number
        }
        result[number] = "\n".join(line for i, line in enumerate(lines) if i not in drop)
    return result


def candidate_pages(pages: Mapping[int, str]) -> list[int]:
    hits = {
        number
        for number, text in pages.items()
        if "fase" in (low := text.casefold()) and any(marker in low for marker in _MARKERS)
    }
    near = {p for n in hits for p in (n - 1, n, n + 1)}
    return sorted(p for p in near if pages.get(p, "").strip())


def page_windows(
    numbers: Sequence[int], pages: Mapping[int, str], max_chars: int
) -> list[list[int]]:
    """Group pages into windows of at most max_chars, repeating one contiguous page between them."""
    windows: list[list[int]] = []
    current: list[int] = []
    size = 0
    for number in numbers:
        length = len(pages[number])
        if current and size + length > max_chars:
            windows.append(current)
            carry = [current[-1]] if current[-1] == number - 1 else []
            current, size = carry, sum(len(pages[p]) for p in carry)
        current.append(number)
        size += length
    if current:
        windows.append(current)
    return windows


def parse_indonesian_date(text: str) -> date | None:
    match = _DATE.search(text)
    if match is None or (month := _MONTHS.get(match.group(2).casefold())) is None:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1)))
    except ValueError:
        return None


def _quoted(text: str, start: int, end: int, pages: dict[int, str]) -> bool:
    try:
        verify_source(text, start, end, pages)
    except ValueError:
        return False
    return True


def _reject(kind: str, item: Mapping[str, Any], reason: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "text": str(item["description"])[:500],
        "reason": reason,
        "page_start": item["page_start"],
        "page_end": item["page_end"],
    }


def _within(statement: Mapping[str, Any], element: Mapping[str, Any]) -> bool:
    return bool(
        statement["description"] in element["description"]
        and element["page_start"]
        <= statement["page_start"]
        <= statement["page_end"]
        <= element["page_end"]
    )


def _merge_element(
    subject: dict[str, Any],
    found: Mapping[str, Any],
    pages: dict[int, str],
    rejected: list[dict[str, Any]],
) -> None:
    item = {
        "description": normalized(found["text"]),
        "page_start": found["page_start"],
        "page_end": found["page_end"],
    }
    if not _quoted(item["description"], item["page_start"], item["page_end"], pages):
        rejected.append(_reject("element", item, "not_in_source"))
        return
    name = normalized(found["element"])[:200]
    if not name:
        rejected.append(_reject("element", item, "unnamed"))
        return
    current = next(
        (e for e in subject["elements"] if e["element"].casefold() == name.casefold()), None
    )
    if current is None:
        current = {"element": name, **item, "statements": []}
        subject["elements"].append(current)
    elif (
        current["description"] in item["description"]
        and current["description"] != item["description"]
    ):
        # A later window saw more of the paragraph: widen it and re-check kept statements.
        current.update(item)
        for statement in [s for s in current["statements"] if not _within(s, current)]:
            current["statements"].remove(statement)
            rejected.append(_reject("statement", statement, "outside_element"))
    seen = {s["description"] for e in subject["elements"] for s in e["statements"]}
    for raw in found["statements"]:
        statement = {
            "description": normalized(raw["text"]),
            "page_start": raw["page_start"],
            "page_end": raw["page_end"],
        }
        if statement["description"] in seen:
            continue
        if not _quoted(
            statement["description"], statement["page_start"], statement["page_end"], pages
        ):
            rejected.append(_reject("statement", statement, "not_in_source"))
        elif not _within(statement, current):
            rejected.append(_reject("statement", statement, "outside_element"))
        else:
            seen.add(statement["description"])
            current["statements"].append(statement)
    current["statements"].sort(
        key=lambda s: (s["page_start"], current["description"].find(s["description"]))
    )


def _without_fragments(statements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        s
        for s in statements
        if not any(
            s["description"] != other["description"] and s["description"] in other["description"]
            for other in statements
        )
    ]


def assemble_draft(
    title: str, outputs: Sequence[Mapping[str, Any]], pages: dict[int, str], max_statements: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Merge per-window AI drafts into one review-shaped draft; only verbatim quotes survive."""
    rejected: list[dict[str, Any]] = []
    subjects: dict[tuple[str, str], dict[str, Any]] = {}
    decree: str | None = None
    effective: date | None = None
    for output in outputs:
        quote = output.get("decree_code")
        if decree is None and quote and _quoted(quote["text"], quote["page"], quote["page"], pages):
            decree = normalized(quote["text"])[:200]
        quote = output.get("effective_on")
        if (
            effective is None
            and quote
            and _quoted(quote["text"], quote["page"], quote["page"], pages)
        ):
            effective = parse_indonesian_date(quote["text"])
        for found in output.get("subjects", []):
            name = normalized(found["name"])[:200]
            if not name:
                for element in found["elements"]:
                    unnamed = {
                        "description": normalized(element["text"]),
                        "page_start": element["page_start"],
                        "page_end": element["page_end"],
                    }
                    rejected.append(_reject("element", unnamed, "unnamed"))
                continue
            subject = subjects.setdefault(
                (name.casefold(), found["phase"]),
                {"name": name, "phase": found["phase"], "elements": []},
            )
            for element in found["elements"]:
                _merge_element(subject, element, pages, rejected)
    kept: list[dict[str, Any]] = []
    count = 0
    for subject in subjects.values():
        elements = []
        for element in subject["elements"]:
            element["statements"] = _without_fragments(element["statements"])
            room = max_statements - count
            for extra in element["statements"][room:]:
                rejected.append(_reject("statement", extra, "limit"))
            element["statements"] = element["statements"][:room]
            if element["statements"]:
                count += len(element["statements"])
                elements.append(element)
            else:
                rejected.append(_reject("element", element, "no_statements"))
        if elements:
            kept.append(subject | {"elements": elements})
    draft = {
        "curriculum": {
            "name": title[:200],
            "decree_code": decree,
            "effective_on": effective.isoformat() if effective else None,
            "is_current": False,
            "subjects": kept,
        },
        "selected_pages": [],
    }
    return draft, {"accepted_statements": count, "rejected": rejected}
