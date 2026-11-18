from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any

# A small set of date/time shapes seen in hand-written ship logs.
_DT_PATTERNS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y.%m.%d %H:%M",
    "%Y年%m月%d日 %H:%M",
    "%Y年%m月%d日%H时%M分",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y.%m.%d",
    "%Y年%m月%d日",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
)

_handlebar = re.compile(r"[（(][^）)]*[）)]")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha1(*parts: Any) -> str:
    h = hashlib.sha1()
    for part in parts:
        h.update(str(part).encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


def short_id(prefix: str, *parts: Any, length: int = 20) -> str:
    return f"{prefix}_{sha1(*parts)[:length]}"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def to_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def from_json(value: str | None, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def normalize_space(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip()


def normalize_header(value: str | None) -> str:
    """Header normalisation used for column aliases.

    Removes units, punctuation and full-width forms.  Percentage signs and
    parenthesised units are dropped *after* the full-width conversion.
    """
    value = unicodedata.normalize("NFKC", value or "").lower()
    value = _handlebar.sub("", value)
    value = re.sub(r"[\s_\-./\\:：,，;；\[\]{}<>\"'`~!@#$%^&*+=|?？。、]", "", value)
    return value.strip()


def normalize_label(value: str | None) -> str:
    """Normalise a bottle label while retaining useful separators.

    Hyphens and spaces are insignificant for matching.  Ellipses used to show
    a copied label fragment are removed.
    """
    value = unicodedata.normalize("NFKC", value or "").upper()
    value = value.replace("…", "").replace("...", "").replace("⋯", "")
    value = re.sub(r"[\s_]+", "", value)
    value = re.sub(r"[-–—−/\\]+", "-", value)
    value = re.sub(r"^-+|-+$", "", value)
    return value


def normalize_station(value: str | None) -> str:
    value = unicodedata.normalize("NFKC", value or "").upper().strip()
    value = re.sub(r"^(CTD|STATION|STN|站次|站位|站号|站)[:：\s]*", "", value)
    value = re.sub(r"[\s_]+", "", value)
    value = value.strip("-/")
    return value


def station_compare(a: str | None, b: str | None) -> tuple[bool, bool]:
    """Return (compatible, exact).

    ``S01`` and ``01`` compare as a numeric fallback (compatible but not
    exact).  Different letter prefixes (A01/B01) do not.
    """
    na, nb = normalize_station(a), normalize_station(b)
    if not na or not nb:
        return True, False
    if na == nb:
        return True, True

    def split(v: str) -> tuple[str, str]:
        m = re.match(r"^([A-Z]*)[-\s]?(\d+)$", v)
        if not m:
            return v, ""
        return m.group(1), str(int(m.group(2)))

    pa, da = split(na)
    pb, db = split(nb)
    if da and db and da == db and (not pa or not pb):
        return True, False
    return False, False


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    text = unicodedata.normalize("NFKC", value).strip()
    text = re.sub(r"\s+", " ", text)
    text = text.replace("T", " ")
    # A time such as 08:10 without a date is not enough for matching.
    for pattern in _DT_PATTERNS:
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            pass
    # ISO timestamps with a timezone suffix.
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except ValueError:
        return None


def parse_float(value: str | None) -> float | None:
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value)).strip()
    text = text.replace(",", ".")
    m = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def excerpt(text: str, start: int, end: int, radius: int = 70) -> str:
    lo = max(0, start - radius)
    hi = min(len(text), end + radius)
    value = normalize_space(text[lo:hi])
    return ("…" if lo else "") + value + ("…" if hi < len(text) else "")
