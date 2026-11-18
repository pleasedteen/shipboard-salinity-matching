"""Shared test helpers.  Uses only the standard library."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.service import MaterialInput, SamplingService  # noqa: E402

FIXTURES = ROOT / "fixtures"
VOYAGE = "KX-26-03"

# Two handover sheets exercise the cross-batch claim rule: the second one
# re-uses a bottle label from the first, so a result must not be claimed twice.
FIXTURE_FILES = (
    ("deck_log.html", "deck"),
    ("deck_log_2.txt", "deck"),
    ("handover.html", "handover"),
    ("lab_results_1.txt", "lab"),
    ("lab_results_2.html", "lab"),
)


def material(name: str, kind: str | None = None, content: str | None = None) -> MaterialInput:
    path = FIXTURES / name
    text = content if content is not None else path.read_text(encoding="utf-8")
    return MaterialInput(name=name, content=text, kind=kind, voyage=VOYAGE)


def fixtures() -> list[MaterialInput]:
    return [material(name, kind) for name, kind in FIXTURE_FILES]


class ServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "sampling.db"
        settings = Settings(
            database_url=f"sqlite:///{self.db_path}", fixture_dir=FIXTURES
        )
        self.service = SamplingService(settings=settings)
        self.addCleanup(self.service.db.close)

    def load(self) -> list:
        return self.service.import_materials(fixtures())

    def batch(self, code: str) -> dict:
        return self.service.get_batch(code)

    def batches(self) -> dict[str, dict]:
        return {b["batch_code"]: b for b in self.service.list_batches(voyage=VOYAGE)}

    def queue(self, status: str = "open") -> list[dict]:
        return self.service.list_review_queue(status=status)["items"]

    def open_items(self, batch_code: str, kind: str | None = None) -> list[dict]:
        return [i for i in self.queue()
                if i["batch_code"] == batch_code and (kind is None or i["kind"] == kind)]
