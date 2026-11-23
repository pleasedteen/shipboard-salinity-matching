"""Build a database in the pre-quarantine ("old library") shape.

The service once accepted a ``path`` field over HTTP and read the file
server-side, storing that path with the material.  Version 1.1 added trust
columns and quarantines every such legacy version until an operator re-sends
the exact bytes from the local CLI.  These helpers recreate a database exactly
as the old build left it, so the migration can be tested without shipping a
binary database in the repository.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.config import Settings
from app.service import SamplingService

from helpers import FIXTURES, VOYAGE, fixtures

TRUST_COLUMNS = ("origin", "trust", "verified_at")
LEGACY_PATH = "/srv/sampling/incoming"


def build_legacy_library(db_path: Path, correct_lab_value: bool = False,
                         withdraw: tuple[str, ...] = (),
                         path_slugs: tuple[str, ...] | None = None) -> None:
    """Write a complete sampling database with the old schema.

    Materials are first imported normally (so extraction, batches and review
    items exist), then the trust columns are dropped and materials are given a
    server-side path, exactly as a legacy path import would leave them.
    ``path_slugs`` narrows this to a subset; the default gives every material a
    path (the old build stored one for both CLI and HTTP imports).  Materials
    left without a path stand in for the old body-only imports and stay
    trusted.
    """
    settings = Settings(database_url=f"sqlite:///{db_path}", fixture_dir=FIXTURES)
    service = SamplingService(settings=settings)
    try:
        service.import_materials(fixtures())
        if correct_lab_value:
            original = (FIXTURES / "lab_results_1.txt").read_text(encoding="utf-8")
            service.revise_material("lab_results_1", original.replace("34.305", "34.355"),
                                    local=True)
        for slug in withdraw:
            service.withdraw_material(slug)
    finally:
        service.db.close()

    # Rewrite the file as the old build would have left it: no trust metadata,
    # and a recorded local path for the versions that were read server-side.
    with sqlite3.connect(db_path) as conn:
        for column in TRUST_COLUMNS:
            conn.execute(f"ALTER TABLE materials DROP COLUMN {column}")
        if path_slugs is None:
            conn.execute("UPDATE materials SET path = ?", (f"{LEGACY_PATH}/material.txt",))
        else:
            marks = ",".join("?" for _ in path_slugs)
            conn.execute(
                f"UPDATE materials SET path = ? WHERE slug IN ({marks})",
                (f"{LEGACY_PATH}/material.txt", *path_slugs),
            )
        conn.commit()


def material_rows(db_path: Path) -> list[dict]:
    """Material rows with trust metadata, whether or not the DB has migrated."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        names = {row[1] for row in conn.execute("PRAGMA table_info(materials)")}
        columns = ["id", "slug", "name", "version", "status", "path"]
        for optional in TRUST_COLUMNS:
            if optional in names:
                columns.append(optional)
        rows = conn.execute(
            f"SELECT {', '.join(columns)} FROM materials ORDER BY slug, version"
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        for column in TRUST_COLUMNS:
            item.setdefault(column, None)
        out.append(item)
    return out


def local_import(service: SamplingService, slug_names: tuple[str, ...] | None = None) -> list:
    """Re-send fixture bytes the way the local CLI does (attested, with path)."""
    items = []
    for item in fixtures():
        stem = Path(item.name).stem
        if slug_names is not None and stem not in slug_names:
            continue
        items.append(type(item)(
            name=item.name,
            content=item.content,
            kind=item.kind,
            voyage=VOYAGE,
            path=f"{LEGACY_PATH}/{item.name}",
            local=True,
        ))
    return service.import_materials(items, trigger="cli")
