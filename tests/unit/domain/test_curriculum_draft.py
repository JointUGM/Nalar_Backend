from datetime import date
from typing import Any

from nalar.domain.curriculum_draft import (
    assemble_draft,
    candidate_pages,
    page_windows,
    parse_indonesian_date,
    strip_running_lines,
)
from nalar.domain.national_references import verify_source

HEADER = "Capaian Pembelajaran Mata Pelajaran IPA Fase D Untuk Jenjang SMP"
WORDS = ["", "satu", "dua", "tiga", "empat", "lima", "enam", "tujuh", "delapan"]
PARAGRAPH = (
    "Pada akhir fase D, peserta didik mampu mengukur besaran fisis. "
    "Peserta didik memahami gerak, gaya dan tekanan."
)
FIRST = "Pada akhir fase D, peserta didik mampu mengukur besaran fisis."
SECOND = "Peserta didik memahami gerak, gaya dan tekanan."


def _pages() -> dict[int, str]:
    pages = {n: f"{HEADER}\n{n}\nIsi halaman {WORDS[n]}." for n in range(1, 9)}
    pages[4] = (
        f"{HEADER}\n4\nElemen Fase D\n"
        "Pemahaman IPA Pada akhir fase D, peserta didik mampu mengukur besaran"
    )
    pages[5] = f"{HEADER}\n5\nfisis. Peserta didik memahami gerak, gaya dan tekanan."
    return pages


def _output(
    text: str,
    statements: list[str],
    start: int = 4,
    end: int = 5,
    decree: dict[str, Any] | None = None,
    element: str = "Pemahaman IPA",
    subject: str = "Ilmu  Pengetahuan Alam",
) -> dict[str, Any]:
    return {
        "decree_code": decree,
        "effective_on": None,
        "subjects": [
            {
                "name": subject,
                "phase": "D",
                "elements": [
                    {
                        "element": element,
                        "text": text,
                        "page_start": start,
                        "page_end": end,
                        "statements": [
                            {"text": s, "page_start": start, "page_end": end} for s in statements
                        ],
                    }
                ],
            }
        ],
    }


def test_running_header_quote_crosses_pages() -> None:
    pages = strip_running_lines(_pages(), 0.5)
    assert HEADER not in pages[4] and "\n5\n" not in f"\n{pages[5]}\n"
    verify_source(PARAGRAPH, 4, 5, pages)


def test_short_documents_keep_every_line() -> None:
    pages = {1: "Judul\nA", 2: "Judul\nB"}
    assert strip_running_lines(pages, 0.5) == pages


def test_candidate_pages_keep_cp_pages_and_neighbours() -> None:
    pages = strip_running_lines(_pages(), 0.5)
    assert candidate_pages(pages) == [3, 4, 5]


def test_windows_overlap_one_contiguous_page_and_respect_size() -> None:
    pages = {1: "a" * 10, 2: "b" * 10, 3: "c" * 10, 5: "e" * 10}
    assert page_windows([1, 2, 3, 5], pages, 20) == [[1, 2], [2, 3], [5]]


def test_indonesian_dates() -> None:
    assert parse_indonesian_date("16 Juli 2025") == date(2025, 7, 16)
    assert parse_indonesian_date("ditetapkan 1 Desember 2024") == date(2024, 12, 1)
    assert parse_indonesian_date("31 Februari 2025") is None
    assert parse_indonesian_date("Juli 2025") is None


def test_overlap_windows_merge_without_duplicates() -> None:
    pages = strip_running_lines(_pages(), 0.5)
    draft, report = assemble_draft(
        "CP IPA",
        [_output(PARAGRAPH, [FIRST, SECOND]), _output(PARAGRAPH, [SECOND])],
        pages,
        2048,
    )
    (subject,) = draft["curriculum"]["subjects"]
    assert subject["name"] == "Ilmu Pengetahuan Alam"
    assert [s["description"] for s in subject["elements"][0]["statements"]] == [FIRST, SECOND]
    assert report == {"accepted_statements": 2, "rejected": []}


def test_paraphrase_and_outside_text_are_reported_not_drafted() -> None:
    pages = strip_running_lines(_pages(), 0.5)
    draft, report = assemble_draft(
        "CP IPA",
        [_output(PARAGRAPH, [FIRST, "Peserta didik memahami gaya.", "Elemen Fase D"])],
        pages,
        2048,
    )
    statements = draft["curriculum"]["subjects"][0]["elements"][0]["statements"]
    assert [s["description"] for s in statements] == [FIRST]
    assert [r["reason"] for r in report["rejected"]] == ["not_in_source", "outside_element"]


def test_draft_never_sets_current_or_unquoted_metadata() -> None:
    pages = strip_running_lines(_pages(), 0.5)
    draft, _ = assemble_draft(
        "CP IPA",
        [_output(PARAGRAPH, [FIRST], decree={"text": "046/H/KR/2025", "page": 4})],
        pages,
        2048,
    )
    assert draft["curriculum"]["is_current"] is False
    assert draft["curriculum"]["decree_code"] is None
    assert draft["curriculum"]["effective_on"] is None


def test_statement_limit_is_reported() -> None:
    pages = strip_running_lines(_pages(), 0.5)
    _, report = assemble_draft("CP", [_output(PARAGRAPH, [FIRST, SECOND])], pages, 1)
    assert report["accepted_statements"] == 1
    assert [r["reason"] for r in report["rejected"]] == ["limit"]


FRAGMENT = "Pada akhir fase D, peserta didik mampu mengukur besaran"


def test_sentence_cut_at_a_window_edge_is_dropped_once_the_full_sentence_arrives() -> None:
    pages = strip_running_lines(_pages(), 0.5)
    draft, report = assemble_draft(
        "CP IPA",
        [
            _output(FRAGMENT, [FRAGMENT], start=4, end=4),
            _output(PARAGRAPH, [FIRST, SECOND]),
        ],
        pages,
        2048,
    )
    statements = draft["curriculum"]["subjects"][0]["elements"][0]["statements"]
    assert [s["description"] for s in statements] == [FIRST, SECOND]
    assert report["accepted_statements"] == 2


def test_unnamed_elements_and_subjects_are_reported_not_drafted() -> None:
    pages = strip_running_lines(_pages(), 0.5)
    draft, report = assemble_draft(
        "CP IPA",
        [
            _output(PARAGRAPH, [FIRST], element="  "),
            _output(PARAGRAPH, [FIRST], subject=""),
        ],
        pages,
        2048,
    )
    assert draft["curriculum"]["subjects"] == []
    assert [r["reason"] for r in report["rejected"]] == ["unnamed", "unnamed"]


def test_a_number_inside_a_sentence_is_not_mistaken_for_a_page_number() -> None:
    pages = {n: f"Isi unik {WORDS[n]}.\n{n}" for n in range(1, 7)}
    pages[2] = "Bilangan cacah sampai\n100\ndan operasinya.\n2"
    stripped = strip_running_lines(pages, 0.5)
    assert stripped[2] == "Bilangan cacah sampai\n100\ndan operasinya."
    assert stripped[1] == "Isi unik satu."


def test_a_lone_number_at_a_page_edge_is_kept_when_pages_are_not_numbered() -> None:
    pages = {n: f"Isi unik {WORDS[n]}." for n in range(1, 7)}
    pages[3] = "Bilangan cacah sampai\n100"
    assert strip_running_lines(pages, 0.5)[3] == "Bilangan cacah sampai\n100"
