import random
import secrets
from typing import Final

ALPHABET: Final = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
LENGTH: Final = 6
_SYSTEM_RANDOM = secrets.SystemRandom()


def generate(rng: random.Random = _SYSTEM_RANDOM) -> str:
    return "".join(rng.choice(ALPHABET) for _ in range(LENGTH))


def normalize(raw: str) -> str | None:
    code = raw.strip().upper()
    if len(code) != LENGTH or not set(code) <= set(ALPHABET):
        return None
    return code
