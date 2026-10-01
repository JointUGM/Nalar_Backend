import pytest

from nalar.domain.placeholders import (
    NarrativeLexicon,
    PlaceholderError,
    counts_from_snapshot,
    fill,
    quantity_problems,
)

LEX = NarrativeLexicon(
    number_words=frozenset({"satu", "dua", "tiga", "belas"}),
    count_claims=("sebagian besar", "semua siswa"),
    score_terms=("skor", "level"),
)
C = "11111111-1111-4111-8111-111111111111"
M = "22222222-2222-4222-8222-222222222222"
SNAPSHOT = {
    "mission_title": "Gaya",
    "denominator": 28,
    "incomplete_count": 3,
    "concepts": [
        {
            "concept_id": C,
            "name": "Gaya gesek",
            "mastered_count": 12,
            "developing_count": 8,
            "not_observed_count": 1,
            "misconceptions": [
                {"misconception_id": M, "statement": "x", "count": 7, "resolved_count": 2}
            ],
        }
    ],
}


def test_fill_uses_the_snapshot_counts() -> None:
    text = (
        f"{{{{count:{M}}}}} dari {{{{total}}}} siswa masih keliru, {{{{resolved:{M}}}}} berubah;"
        f" {{{{mastered:{C}}}}} menguasai; {{{{incomplete}}}} belum selesai."
    )
    assert fill(text, counts_from_snapshot(SNAPSHOT), LEX) == (
        "7 dari 28 siswa masih keliru, 2 berubah; 12 menguasai; 3 belum selesai."
    )


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("Ada 7 siswa yang keliru.", "digit"),
        ("Tiga siswa masih keliru.", "number_word:tiga"),
        ("Dua belas siswa keliru.", "number_word:belas"),
        ("Sebagian besar siswa keliru.", "count_claim:sebagian besar"),
        ("Hasil {{total}", "malformed_placeholder"),
    ],
)
def test_quantities_outside_placeholders_are_rejected(text: str, problem: str) -> None:
    assert problem in quantity_problems(text, LEX)
    with pytest.raises(PlaceholderError):
        fill(text, counts_from_snapshot(SNAPSHOT), LEX)


def test_words_that_merely_contain_a_number_word_pass() -> None:
    assert quantity_problems("Kedua konsep memakai satuan newton.", LEX) == []


def test_a_placeholder_for_an_id_outside_the_snapshot_is_rejected() -> None:
    other = "33333333-3333-4333-8333-333333333333"
    with pytest.raises(PlaceholderError, match=f"unknown_key:count:{other}"):
        fill(f"{{{{count:{other}}}}} siswa", counts_from_snapshot(SNAPSHOT), LEX)


def test_score_terms_are_rejected_only_in_parent_text() -> None:
    text = "Anak Anda mencapai level baik."
    assert quantity_problems(text, LEX) == []
    assert quantity_problems(text, LEX, parent=True) == ["score_term:level"]
