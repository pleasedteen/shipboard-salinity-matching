from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_url: str
    fixture_dir: Path
    timezone_note: str = "naive local ship time"

    @classmethod
    def from_env(cls, database_url: str | None = None) -> "Settings":
        url = database_url or os.environ.get("DATABASE_URL", "")
        if not url:
            default = Path(os.environ.get("SAMPLING_DB", "var/sampling.db"))
            url = "sqlite:///" + str(default)
        fixture = Path(os.environ.get("SAMPLING_FIXTURES", "fixtures"))
        return cls(database_url=url, fixture_dir=fixture)


def redact_url(url: str) -> str:
    """Return a log-safe database URL."""
    if "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    if "@" not in rest:
        return url
    creds, host = rest.rsplit("@", 1)
    user = creds.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"
