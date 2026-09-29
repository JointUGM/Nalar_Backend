"""Write openapi.json for the frontend (B20). --check exits 1 when the file is stale."""

import argparse
import sys
from pathlib import Path

from nalar.bootstrap.openapi import render_openapi

OUTPUT = Path(__file__).resolve().parents[1] / "openapi.json"

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    text = render_openapi()
    if parser.parse_args().check:
        sys.exit(0 if OUTPUT.exists() and OUTPUT.read_text(encoding="utf-8") == text else 1)
    OUTPUT.write_text(text, encoding="utf-8", newline="\n")
