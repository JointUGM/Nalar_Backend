import re
import unicodedata

_NOT_ALNUM = re.compile(r"[^a-z0-9]+")


def topic_key(title: str) -> str:
    folded = unicodedata.normalize("NFKD", title.casefold())
    ascii_only = "".join(c for c in folded if not unicodedata.combining(c))
    key = _NOT_ALNUM.sub("-", ascii_only).strip("-")
    if not key:
        raise ValueError("topic title has no letters or digits")
    return key[:80]
