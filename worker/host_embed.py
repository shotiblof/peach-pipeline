"""Build embed URLs for Vidara / DoodStream filecodes."""
from __future__ import annotations

VIDARA_EMBED = "https://vidara.so/e/"
DOOD_EMBED = "https://dood.la/e/"


def normalize_filecode(filecode: str) -> str:
    value = (filecode or "").strip()
    if "/e/" in value:
        return value.rsplit("/e/", 1)[-1].strip("/")
    if "/d/" in value:
        return value.rsplit("/d/", 1)[-1].strip("/")
    return value


def embed_url(filecode: str, host_provider: str = "vidara") -> str:
    fc = normalize_filecode(filecode)
    if not fc:
        return ""
    if host_provider == "doodstream":
        return f"{DOOD_EMBED}{fc}"
    return f"{VIDARA_EMBED}{fc}"


def is_vidara_rate_or_hard_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    if "429" in text:
        return True
    if "rate limit" in text:
        return True
    if "vidara" in text and any(token in text for token in ("http 5", "http 4", "failed", "missing")):
        return True
    return False
