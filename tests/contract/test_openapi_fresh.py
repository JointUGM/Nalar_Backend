from pathlib import Path

from nalar.bootstrap.openapi import render_openapi

OPENAPI = Path(__file__).resolve().parents[2] / "openapi.json"


def test_openapi_json_is_up_to_date() -> None:
    assert OPENAPI.read_text(encoding="utf-8") == render_openapi(), (
        "routes or schemas changed: run `uv run python scripts/export_openapi.py`"
        " and commit openapi.json"
    )
