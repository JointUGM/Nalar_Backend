import tomllib
from pathlib import Path

from nalar.domain.integrity import IntegrityConfig

CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"


def load_integrity_config(path: Path = CONFIG_DIR / "integrity.toml") -> IntegrityConfig:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    paste, tab = raw["large_paste"], raw["tab_switching"]
    gap, disc = raw["inconsistency_gap"], raw["disconnect_pattern"]
    sim = raw["cross_student_similarity"]
    return IntegrityConfig(
        version=int(raw["version"]),
        paste_min_chars=paste["min_chars"],
        paste_min_share=paste["min_share"],
        paste_severity=paste["severity"],
        hidden_ms_per_answer=tab["min_hidden_ms_per_answer"],
        hidden_events_per_session=tab["min_hidden_events_per_session"],
        tab_severity=tab["severity"],
        gap_first_min_quality=gap["first_min_quality"],
        gap_next_max_quality=gap["next_max_quality"],
        gap_severity=gap["severity"],
        disconnect_min_jump=disc["min_quality_jump"],
        disconnect_severity=disc["severity"],
        similarity_min_jaccard=sim["min_jaccard"],
        similarity_min_tokens=sim["min_tokens"],
        similarity_severity=sim["severity"],
    )
