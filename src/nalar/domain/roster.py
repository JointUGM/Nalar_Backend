import csv
import io
import re
from collections.abc import Mapping
from dataclasses import dataclass

COLUMNS = (
    "role",
    "full_name",
    "email",
    "nisn",
    "class_name",
    "grade_level",
    "parent_email",
    "parent_name",
    "relationship",
)
_NISN = re.compile(r"[0-9]{10}")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


@dataclass(frozen=True)
class RosterRow:
    row_number: int
    role: str
    full_name: str
    email: str | None
    nisn: str | None
    class_name: str | None
    grade_level: int | None
    parent_email: str | None
    parent_name: str | None
    relationship: str | None


@dataclass(frozen=True)
class RowError:
    row_number: int
    field: str
    message: str


def _clean(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _check(raw: Mapping[str, str | None], seen_nisn: set[str]) -> tuple[str, str] | None:
    role, name = _clean(raw["role"]), _clean(raw["full_name"])
    email, parent_email = _clean(raw["email"]), _clean(raw["parent_email"])
    if role not in ("student", "teacher"):
        return "role", "Peran harus student atau teacher."
    if not name:
        return "full_name", "Nama wajib diisi."
    if email and not _EMAIL.fullmatch(email):
        return "email", "Format email tidak valid."
    if parent_email and not _EMAIL.fullmatch(parent_email):
        return "parent_email", "Format email orang tua tidak valid."
    if role == "teacher":
        return None if email else ("email", "Email guru wajib diisi.")
    nisn = _clean(raw["nisn"])
    if not nisn or not _NISN.fullmatch(nisn):
        return "nisn", "NISN wajib diisi dan terdiri dari 10 angka."
    if nisn in seen_nisn:
        return "nisn", "NISN muncul lebih dari sekali di berkas ini."
    if not _clean(raw["class_name"]):
        return "class_name", "Kelas wajib diisi."
    grade = _clean(raw["grade_level"])
    if not grade or not grade.isascii() or not grade.isdecimal() or not 1 <= int(grade) <= 12:
        return "grade_level", "Tingkat kelas harus angka 1 sampai 12."
    return None


def parse_roster(text: str) -> tuple[list[RosterRow], list[RowError]]:
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        header = next(reader, [])
    except csv.Error:
        return [], [RowError(1, "header", "Format header CSV tidak valid.")]
    missing = [c for c in COLUMNS if c not in header]
    if missing:
        return [], [RowError(1, "header", f"Kolom tidak ada: {', '.join(missing)}.")]
    if len(set(header)) != len(header):
        return [], [RowError(1, "header", "Nama kolom tidak boleh berulang.")]
    rows: list[RosterRow] = []
    errors: list[RowError] = []
    seen_nisn: set[str] = set()
    while True:
        number = reader.line_num + 1
        try:
            values = next(reader)
        except StopIteration:
            break
        except csv.Error:
            errors.append(RowError(number, "row", "Format baris CSV tidak valid."))
            break
        if not values:
            continue
        if len(values) != len(header):
            errors.append(RowError(number, "row", "Jumlah nilai berbeda dari jumlah kolom."))
            continue
        raw = dict(zip(header, values, strict=True))
        problem = _check(raw, seen_nisn)
        if problem:
            errors.append(RowError(number, *problem))
            continue
        nisn = _clean(raw["nisn"]) if _clean(raw["role"]) == "student" else None
        if nisn:
            seen_nisn.add(nisn)
        grade = _clean(raw["grade_level"]) if _clean(raw["role"]) == "student" else None
        rows.append(
            RosterRow(
                row_number=number,
                role=str(_clean(raw["role"])),
                full_name=str(_clean(raw["full_name"])),
                email=(_clean(raw["email"]) or "").lower() or None,
                nisn=nisn if _clean(raw["role"]) == "student" else None,
                class_name=_clean(raw["class_name"]),
                grade_level=int(grade) if grade else None,
                parent_email=(_clean(raw["parent_email"]) or "").lower() or None,
                parent_name=_clean(raw["parent_name"]),
                relationship=_clean(raw["relationship"]),
            )
        )
    return rows, errors
