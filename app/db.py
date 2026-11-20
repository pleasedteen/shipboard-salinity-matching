from __future__ import annotations

import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from .config import redact_url

SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS materials (
        id TEXT PRIMARY KEY,
        slug TEXT NOT NULL,
        voyage TEXT NOT NULL DEFAULT 'default',
        kind TEXT NOT NULL,
        name TEXT NOT NULL,
        path TEXT,
        content TEXT NOT NULL,
        content_sha TEXT NOT NULL,
        status TEXT NOT NULL,
        version INTEGER NOT NULL,
        imported_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_materials_voyage ON materials(voyage, slug, status)",
    "CREATE INDEX IF NOT EXISTS idx_materials_sha ON materials(content_sha)",
    """
    CREATE TABLE IF NOT EXISTS raw_records (
        id TEXT PRIMARY KEY,
        material_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        record_key TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        payload TEXT NOT NULL,
        locator TEXT NOT NULL,
        confidence REAL NOT NULL,
        fingerprint TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_material_key ON raw_records(material_id, record_key)",
    "CREATE INDEX IF NOT EXISTS idx_raw_key ON raw_records(record_key)",
    """
    CREATE TABLE IF NOT EXISTS batches (
        id TEXT PRIMARY KEY,
        voyage TEXT NOT NULL DEFAULT 'default',
        batch_code TEXT NOT NULL,
        station TEXT,
        sample_start TEXT,
        sample_end TEXT,
        status TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 0,
        fingerprint TEXT,
        reason TEXT,
        handover_material_id TEXT,
        source_material_id TEXT,
        last_run_id TEXT,
        bottle_count INTEGER NOT NULL DEFAULT 0,
        published_count INTEGER NOT NULL DEFAULT 0,
        pending_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_batches_code ON batches(voyage, batch_code)",
    """
    CREATE TABLE IF NOT EXISTS batch_bottles (
        id TEXT PRIMARY KEY,
        batch_id TEXT NOT NULL,
        voyage TEXT NOT NULL DEFAULT 'default',
        ordinal INTEGER NOT NULL,
        handover_key TEXT,
        record_key TEXT NOT NULL,
        label_raw TEXT,
        label_norm TEXT,
        station TEXT,
        sampled_at TEXT,
        sample_end TEXT,
        locator TEXT,
        confidence REAL NOT NULL,
        fingerprint TEXT NOT NULL,
        payload TEXT
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_bottles_batch_key ON batch_bottles(batch_id, record_key)",
    """
    CREATE TABLE IF NOT EXISTS lab_results (
        id TEXT PRIMARY KEY,
        record_key TEXT NOT NULL,
        material_id TEXT NOT NULL,
        voyage TEXT NOT NULL DEFAULT 'default',
        batch_hint TEXT,
        sample_no TEXT,
        label_raw TEXT,
        label_norm TEXT,
        station TEXT,
        sampled_at_raw TEXT,
        sampled_at TEXT,
        sample_end TEXT,
        time_precision TEXT,
        salinity REAL,
        locator TEXT,
        confidence REAL NOT NULL,
        fingerprint TEXT NOT NULL,
        present INTEGER NOT NULL DEFAULT 1,
        active INTEGER NOT NULL DEFAULT 1,
        payload TEXT,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_lab_key ON lab_results(record_key)",
    "CREATE INDEX IF NOT EXISTS idx_lab_active ON lab_results(voyage, active)",
    """
    CREATE TABLE IF NOT EXISTS batch_inputs (
        batch_id TEXT NOT NULL,
        material_id TEXT NOT NULL,
        role TEXT NOT NULL,
        material_version INTEGER NOT NULL,
        record_key TEXT NOT NULL,
        linked_at TEXT NOT NULL,
        PRIMARY KEY (batch_id, material_id, record_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_batch_inputs_material ON batch_inputs(material_id)",
    """
    CREATE TABLE IF NOT EXISTS batch_results (
        id TEXT PRIMARY KEY,
        batch_id TEXT NOT NULL,
        batch_version INTEGER NOT NULL,
        bottle_id TEXT NOT NULL,
        lab_result_id TEXT NOT NULL,
        salinity REAL,
        state TEXT NOT NULL,
        extraction_confidence REAL,
        matching_confidence REAL,
        overall_confidence REAL,
        decision_id TEXT,
        payload TEXT,
        fingerprint TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_bresult_bottle ON batch_results(batch_id, batch_version, bottle_id)",
    "CREATE INDEX IF NOT EXISTS idx_bresult_current ON batch_results(batch_id, batch_version)",
    """
    CREATE TABLE IF NOT EXISTS review_decisions (
        id TEXT PRIMARY KEY,
        decision_key TEXT NOT NULL,
        kind TEXT NOT NULL,
        voyage TEXT NOT NULL DEFAULT 'default',
        batch_id TEXT,
        batch_code TEXT,
        record_key TEXT,
        field TEXT,
        context_fingerprint TEXT NOT NULL,
        value TEXT NOT NULL,
        reviewer TEXT,
        note TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'active',
        invalidated_at TEXT,
        invalidated_reason TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_decisions_key ON review_decisions(decision_key)",
    "CREATE INDEX IF NOT EXISTS idx_decisions_batch ON review_decisions(batch_id, kind, active)",
    """
    CREATE TABLE IF NOT EXISTS review_items (
        id TEXT PRIMARY KEY,
        scope TEXT NOT NULL,
        voyage TEXT NOT NULL DEFAULT 'default',
        batch_id TEXT,
        batch_version INTEGER,
        kind TEXT NOT NULL,
        status TEXT NOT NULL,
        title TEXT NOT NULL,
        detail TEXT,
        subject_key TEXT NOT NULL,
        record_key TEXT,
        field TEXT,
        payload TEXT NOT NULL,
        candidates TEXT NOT NULL,
        locator TEXT,
        source_material TEXT,
        context_fingerprint TEXT,
        decision_id TEXT,
        reviewer TEXT,
        note TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_review_subject ON review_items(subject_key)",
    "CREATE INDEX IF NOT EXISTS idx_review_queue ON review_items(status, batch_id)",
    """
    CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY,
        trigger TEXT NOT NULL,
        status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        summary TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS page_sets (
        id TEXT PRIMARY KEY,
        voyage TEXT NOT NULL,
        slug TEXT NOT NULL,
        version INTEGER NOT NULL,
        expected_pages INTEGER NOT NULL,
        format TEXT NOT NULL DEFAULT 'text',
        kind TEXT NOT NULL DEFAULT 'deck',
        name TEXT,
        status TEXT NOT NULL,
        material_id TEXT,
        page_map TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_pageset_key ON page_sets(voyage, slug, version)",
    "CREATE INDEX IF NOT EXISTS idx_pageset_material ON page_sets(material_id)",
    """
    CREATE TABLE IF NOT EXISTS material_pages (
        id TEXT PRIMARY KEY,
        page_set_id TEXT NOT NULL,
        page_no INTEGER NOT NULL,
        name TEXT NOT NULL,
        path TEXT,
        content TEXT NOT NULL,
        content_sha TEXT NOT NULL,
        received_at TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_material_pages_no "
    "ON material_pages(page_set_id, page_no)",
)


class Database:
    """DB-API wrapper for SQLite (embedded) and PostgreSQL (production).

    Application SQL uses ``?`` placeholders; PostgreSQL statements are
    translated to ``%s`` automatically.  A single connection guarded by an
    RLock is sufficient for this review-service workload and keeps the embedded
    deployment dependency-free.
    """

    def __init__(self, url: str):
        self.url = url
        self.is_sqlite = url.startswith("sqlite:")
        self._lock = threading.RLock()
        self._depth = 0
        self._schema_ready = False
        if self.is_sqlite:
            self._conn = self._open_sqlite()
        else:
            self._conn = self._open_postgres()

    # -- connection ----------------------------------------------------------
    def _open_sqlite(self) -> sqlite3.Connection:
        raw = self.url.split("sqlite://", 1)[-1]
        if raw in ("", ":memory:"):
            path = ":memory:"
        elif raw.startswith("//"):
            path = raw[1:]
        else:
            path = raw
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False, timeout=30,
                               isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _open_postgres(self) -> Any:
        try:
            import psycopg  # type: ignore
        except ImportError:  # pragma: no cover
            try:
                import psycopg2 as psycopg  # type: ignore
            except ImportError as exc:
                raise RuntimeError(
                    "PostgreSQL requires the 'psycopg' driver "
                    "(pip install 'psycopg[binary]'). "
                    f"Configured URL: {redact_url(self.url)}"
                ) from exc
        return psycopg.connect(self.url, autocommit=False)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    # -- SQL helpers ---------------------------------------------------------
    def sql(self, query: str) -> str:
        """Translate the application's ``?`` placeholders for PostgreSQL.

        SQL is written in the most widely supported dialect (no SQLite
        extensions, no dialect-specific functions); only the placeholder style
        differs, plus booleans which are bound as integers and therefore need
        no conversion.
        """
        return query if self.is_sqlite else query.replace("?", "%s")

    def _execute(self, query: str, params: Iterable[Any] = ()) -> Any:
        if self._conn is None:
            raise RuntimeError("database connection is closed")
        cur = self._conn.cursor()
        cur.execute(self.sql(query), tuple(params))
        return cur

    def fetchall(self, query: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._execute(query, params)
            rows = cur.fetchall()
            names = [d[0] for d in cur.description]
            cur.close()
            return [dict(zip(names, row)) for row in rows]

    def fetchone(self, query: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        rows = self.fetchall(query, params)
        return rows[0] if rows else None

    def scalar(self, query: str, params: Iterable[Any] = (), default: Any = None) -> Any:
        row = self.fetchone(query, params)
        if not row:
            return default
        return next(iter(row.values()))

    def execute(self, query: str, params: Iterable[Any] = ()) -> int:
        with self._lock:
            cur = self._execute(query, params)
            count = cur.rowcount
            cur.close()
            return count

    def insert(self, table: str, values: dict[str, Any]) -> None:
        cols = list(values)
        marks = ", ".join("?" for _ in cols)
        self.execute(
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({marks})",
            [values[c] for c in cols],
        )

    def update(self, table: str, ident: str, values: dict[str, Any]) -> None:
        if not values:
            return
        sets = ", ".join(f"{c} = ?" for c in values)
        self.execute(
            f"UPDATE {table} SET {sets} WHERE id = ?",
            list(values.values()) + [ident],
        )

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Re-entrant transaction; the outermost block commits or rolls back."""
        with self._lock:
            outermost = self._depth == 0
            if outermost:
                if self.is_sqlite:
                    self._conn.execute("BEGIN IMMEDIATE")
                else:  # pragma: no cover - exercises only with a live server
                    self._conn.autocommit = False
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                if outermost:
                    self._conn.rollback()
                raise
            else:
                self._depth -= 1
                if outermost:
                    self._conn.commit()

    def ensure_schema(self) -> None:
        with self._lock:
            if self._schema_ready:
                return
            with self.transaction():
                for statement in SCHEMA:
                    self._execute(statement)
            self._schema_ready = True

    def ping(self) -> bool:
        row = self.fetchone("SELECT 1 AS ok")
        return bool(row and row["ok"] == 1)


def is_postgres_url(url: str) -> bool:
    return bool(re.match(r"^(postgresql|postgres)://", url or ""))
