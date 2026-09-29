import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PINNED = ROOT / "contracts" / "nalar-ai.openapi.json"
UPSTREAM = ROOT.parent / "Nalar_AI" / "openapi.json"


@pytest.mark.skipif(not UPSTREAM.exists(), reason="Nalar_AI is not checked out next to this repo")
def test_pinned_ai_contract_matches_the_ai_repo() -> None:
    pinned = json.loads(PINNED.read_text(encoding="utf-8"))
    upstream = json.loads(UPSTREAM.read_text(encoding="utf-8"))
    assert pinned == upstream, (
        "Nalar_AI/openapi.json changed: copy it to contracts/ and run scripts/gen_ai_client.py"
    )
