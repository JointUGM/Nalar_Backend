import pytest

from nalar.domain.national_references import verify_source


def test_cp_statement_must_be_present_on_cited_pages() -> None:
    with pytest.raises(ValueError, match="SOURCE_TEXT_MISMATCH"):
        verify_source(
            "Peserta didik menjelaskan energi.", 1, 1, {1: "Peserta didik menjelaskan gaya."}
        )


def test_source_matching_normalizes_pdf_line_breaks() -> None:
    verify_source(
        "Peserta didik menjelaskan gaya.", 1, 2, {1: "Peserta didik\nmenjelaskan", 2: "gaya."}
    )


def test_source_cannot_point_at_missing_or_empty_pages() -> None:
    with pytest.raises(ValueError, match="SOURCE_PAGE_INVALID"):
        verify_source("Gaya", 1, 2, {1: "Gaya", 2: ""})


def test_statement_cannot_be_empty() -> None:
    with pytest.raises(ValueError, match="SOURCE_TEXT_MISMATCH"):
        verify_source("  ", 1, 1, {1: "Gaya"})


def test_a_line_break_after_a_hyphen_is_not_content() -> None:
    verify_source(
        "Peserta didik melakukan langkah-langkah operasional.",
        1,
        2,
        {1: "Peserta didik melakukan langkah-\nlangkah", 2: "operasional."},
    )


def test_a_dropped_hyphen_is_still_a_mismatch() -> None:
    with pytest.raises(ValueError, match="SOURCE_TEXT_MISMATCH"):
        verify_source(
            "Peserta didik melakukan langkah langkah operasional.",
            1,
            1,
            {1: "Peserta didik melakukan langkah-\nlangkah operasional."},
        )


TABLE_PAGES = {
    1: "Elemen Fase D\nPemahaman IPA Pada akhir fase D, peserta didik mengukur suhu.",
    2: "Elemen Fase D\nPeserta didik memahami gerak.\nPeserta didik membuat rangkaian listrik.",
    3: "Elemen Fase D\nMengamati benda di sekitar.",
}


def test_a_table_header_continued_at_a_page_join_is_layout() -> None:
    # The 2022 CP IPA booklet repeats "Elemen Fase D" atop each continued page of its CP table.
    verify_source(
        "Pada akhir fase D, peserta didik mengukur suhu. Peserta didik memahami gerak.",
        1,
        2,
        TABLE_PAGES,
    )


def test_only_the_join_may_skip_a_repeated_line() -> None:
    with pytest.raises(ValueError, match="SOURCE_TEXT_MISMATCH"):
        verify_source("Pemahaman IPA", 1, 2, {1: "Pemahaman\nElemen\nIPA", 2: "Elemen\nGaya."})


def test_a_heading_under_the_continued_header_is_content_even_when_repeated() -> None:
    pages = {
        1: "Elemen | Deskripsi\nMengamati\nMenjelaskan cara mengamati.",
        2: "Elemen Fase D\nPeserta didik mengamati.",
        3: "Elemen Fase D\nMengamati\nMenggunakan alat bantu.",
    }
    verify_source("Peserta didik mengamati. Mengamati Menggunakan alat bantu.", 2, 3, pages)


def test_a_sentence_at_a_page_join_is_content_even_when_repeated() -> None:
    pages = {
        1: "Peserta didik mengukur suhu.",
        2: "Bagus.\nPeserta didik memahami gerak.",
        3: "Bagus.",
    }
    with pytest.raises(ValueError, match="SOURCE_TEXT_MISMATCH"):
        verify_source("Peserta didik mengukur suhu. Peserta didik memahami gerak.", 1, 2, pages)


def test_a_line_on_only_one_page_is_content() -> None:
    pages = {1: "Peserta didik mengukur suhu", 2: "Elemen Fase D\nPeserta didik memahami gerak."}
    with pytest.raises(ValueError, match="SOURCE_TEXT_MISMATCH"):
        verify_source("Peserta didik mengukur suhu Peserta didik memahami gerak.", 1, 2, pages)
