"""Exercise the PostgreSQL code path without a server.

The production driver is replaced by a shim that maps ``%s`` placeholders back
onto an in-memory SQLite database.  This proves that every statement issued in
production mode is dialect-translated, transactional and schema-compatible; it
does not replace a real integration run against PostgreSQL.
"""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path

from helpers import FIXTURES, fixtures

from app.config import Settings
from app.db import Database
from app.service import MaterialInput, SamplingService


def material_input(name: str, kind: str, voyage: str, **extra) -> MaterialInput:
    path = FIXTURES / "paged_deck" / name
    return MaterialInput(name=name, content=path.read_text("utf-8"), kind=kind,
                         voyage=voyage, **extra)


class _Cursor:
    def __init__(self, cursor, issued: list[str]):
        self._cursor = cursor
        self._issued = issued

    def execute(self, query: str, params=()):
        self._issued.append(query)
        self._cursor.execute(query.replace("%s", "?"), tuple(params))
        return self

    def fetchall(self):
        return self._cursor.fetchall()

    @property
    def description(self):
        return self._cursor.description

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def close(self):
        self._cursor.close()


class _Connection:
    """DB-API facade that only accepts ``%s`` placeholders."""

    def __init__(self, path: str):
        import sqlite3

        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self.autocommit = False
        self.issued: list[str] = []

    def cursor(self):
        return _Cursor(self._conn.cursor(), self.issued)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


class PostgresDialectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.connections: list[_Connection] = []
        module = types.ModuleType("psycopg")

        def connect(url: str, autocommit: bool = False):
            connection = _Connection(str(Path(self.tmp.name) / "pg.db"))
            self.connections.append(connection)
            return connection

        module.connect = connect  # type: ignore[attr-defined]
        self._prior = sys.modules.get("psycopg")
        sys.modules["psycopg"] = module
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._prior is None:
            sys.modules.pop("psycopg", None)
        else:
            sys.modules["psycopg"] = self._prior

    def test_full_pipeline_over_postgres_path(self) -> None:
        service = SamplingService(settings=Settings(
            database_url="postgresql://tester@localhost/sampling",
            fixture_dir=FIXTURES,
        ))
        self.addCleanup(service.db.close)
        self.assertFalse(service.db.is_sqlite)
        reports = service.import_materials(fixtures())
        self.assertEqual(len(reports), 5)

        batches = {b["batch_code"]: b for b in service.list_batches()}
        self.assertEqual(batches["KX-2603-A"]["status"], "published")
        self.assertEqual(batches["KX-2603-E"]["status"], "held")
        self.assertEqual(batches["KX-2603-G"]["status"], "in_review")
        self.assertEqual(service.query_samples(batch="KX-2603-A")["total"], 4)

        item = next(i for i in service.list_review_queue()["items"]
                    if i["kind"] == "ambiguous_assignment")
        service.decide(item["id"], "confirm",
                       value={"result": item["candidates"][0]["result"]})
        self.assertEqual(service.get_batch("KX-2603-G")["status"], "published")

        # No un-translated placeholder may reach the driver.
        for connection in self.connections:
            for query in connection.issued:
                self.assertNotIn("?", query)
            self.assertNotIn("?", str(connection.issued))

    def test_page_collection_over_postgres_path(self) -> None:
        from app.document import PageInput

        paged = FIXTURES / "paged_deck"
        service = SamplingService(settings=Settings(
            database_url="postgresql://tester@localhost/sampling",
            fixture_dir=paged,
        ))
        self.addCleanup(service.db.close)
        voyage = "PG-26-01"
        service.import_materials([
            material_input("handover.html", "handover", voyage),
            material_input("lab_results.txt", "lab", voyage),
        ])
        for number in (3, 1, 2, 4):
            report = service.import_pages([MaterialInput(
                name="deck", content="", kind="deck", voyage=voyage, slug="deck",
                expected_pages=4,
                pages=[PageInput(number, f"deck_p{number}.txt",
                                 (paged / f"deck_p{number}.txt").read_text("utf-8"))],
            )])[0]
        self.assertEqual(report.status, "activated")
        self.assertEqual(service.get_page_set("deck", voyage=voyage)["missing_pages"], [])
        self.assertEqual(service.query_samples(voyage=voyage)["total"], 10)
        for connection in self.connections:
            self.assertEqual([q for q in connection.issued if "?" in q], [])

    def test_missing_driver_raises_actionable_error(self) -> None:
        sys.modules.pop("psycopg", None)
        sys.modules["psycopg"] = None  # type: ignore[assignment]
        with self.assertRaises(RuntimeError) as ctx:
            Database("postgresql://u:p@localhost/db")
        self.assertIn("psycopg", str(ctx.exception))
        self.assertNotIn(":p@", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
