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
