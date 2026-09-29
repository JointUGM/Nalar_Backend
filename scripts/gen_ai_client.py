"""Regenerate the AI contract models from the pinned OpenAPI file."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contracts" / "nalar-ai.openapi.json"
OUTPUT = ROOT / "src" / "nalar" / "application" / "ports" / "ai_contract.py"


def main() -> None:
    subprocess.run(
        [
            sys.executable,
            "-m",
            "datamodel_code_generator",
            "--input",
            str(CONTRACT),
            "--input-file-type",
            "openapi",
            "--output",
            str(OUTPUT),
            "--output-model-type",
            "pydantic_v2.BaseModel",
            "--target-python-version",
            "3.12",
            "--use-annotated",
            "--field-constraints",
            "--use-standard-collections",
            "--use-union-operator",
            "--disable-timestamp",
            "--formatters",
            "ruff-format",
        ],
        check=True,
    )


if __name__ == "__main__":
    main()
