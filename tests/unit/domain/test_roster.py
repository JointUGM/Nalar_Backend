import pytest

from nalar.domain.roster import parse_roster

HEADER = "role,full_name,email,nisn,class_name,grade_level,parent_email,parent_name,relationship"


def csv(*rows: str) -> str:
    return "\n".join([HEADER, *rows]) + "\n"


def test_valid_rows_parse_and_bad_rows_report_their_number() -> None:
    rows, errors = parse_roster(
        csv(
            "student,Budi Santoso,,0012345678,8A,8,ibu.budi@mail.id,Sri,ibu",
            "student,Ani,,,8A,8,,,",
            "teacher,Pak Joko,joko@smp.id,,,,,,",
            "teacher,Tanpa Email,,,,,,,",
            "kepala,Siapa,,,,,,,",
        )
    )
    assert [(r.row_number, r.role) for r in rows] == [(2, "student"), (4, "teacher")]
    assert rows[0].parent_email == "ibu.budi@mail.id"
    assert [(e.row_number, e.field) for e in errors] == [(3, "nisn"), (5, "email"), (6, "role")]


def test_nisn_must_be_ten_digits_and_unique_in_the_file() -> None:
    _, errors = parse_roster(
        csv(
            "student,A,,12345,8A,8,,,",
            "student,B,,0012345678,8A,8,,,",
            "student,C,,0012345678,8B,8,,,",
        )
    )
    assert [(e.row_number, e.field) for e in errors] == [(2, "nisn"), (4, "nisn")]


def test_a_missing_column_is_one_header_error() -> None:
    rows, errors = parse_roster("role,full_name\nstudent,A\n")
    assert rows == []
    assert [(e.row_number, e.field) for e in errors] == [(1, "header")]


@pytest.mark.parametrize(
    "row,field",
    [
        ("student,A,,0012345678,8A,²,,,", "grade_level"),
        ("student,A,,٠٠١٢٣٤٥٦٧٨,8A,8,,,", "nisn"),
        ("student,A,,0012345678,8A,8,,,,extra", "row"),
        ('"unclosed', "row"),
    ],
)
def test_malformed_rows_are_reported_without_crashing(row: str, field: str) -> None:
    rows, errors = parse_roster(csv(row))
    assert rows == []
    assert [(e.row_number, e.field) for e in errors] == [(2, field)]


def test_error_number_uses_the_csv_line_after_blank_and_multiline_rows() -> None:
    rows, errors = parse_roster(
        csv("", 'teacher,"Pak\nJoko",joko@smp.id,,,,,,', "student,A,,,8A,8,,,")
    )
    assert rows[0].row_number == 3
    assert errors[0].row_number == 5
