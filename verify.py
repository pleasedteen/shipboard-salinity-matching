#!/usr/bin/env python3
"""One-command verification for the sampling service.

Runs the complete unit/integration suite against the embedded SQLite store,
then a fixture walk-through that prints a human-readable summary.  No network
access is required.

    python verify.py

Exit code 0 means every check passed.  Typical runtime is a few seconds.
"""

from __future__ import annotations

import io
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from app.config import Settings  # noqa: E402
from app.document import PageInput  # noqa: E402
from app.service import MaterialInput, SamplingService  # noqa: E402

FIXTURE_FILES = (
    ("deck_log.html", "deck"),
    ("deck_log_2.txt", "deck"),
    ("handover.html", "handover"),
    ("lab_results_1.txt", "lab"),
    ("lab_results_2.html", "lab"),
)
VOYAGE = "KX-26-03"


def run_tests() -> tuple[bool, str]:
    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT / "tests"))
    stream = io.StringIO()
    with redirect_stdout(stream), redirect_stderr(stream):
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    output = stream.getvalue()
    ok = result.wasSuccessful()
    return ok, output


def demo() -> str:
    lines: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        service = SamplingService(settings=Settings(
            database_url=f"sqlite:///{Path(tmp) / 'demo.db'}",
            fixture_dir=ROOT / "fixtures",
        ))
        try:
            materials = [
                MaterialInput(name=name, content=(ROOT / "fixtures" / name).read_text("utf-8"),
                              kind=kind, voyage=VOYAGE)
                for name, kind in FIXTURE_FILES
            ]
            reports = service.import_materials(materials)
            lines.append("1. Imported materials")
            for report in reports:
                lines.append(
                    f"   - {report.name:<22} {report.kind:<9} "
                    f"{report.records:>2} records  status={report.status}"
                )

            lines.append("")
            lines.append("2. Batches after import")
            for batch in service.list_batches(voyage=VOYAGE):
                lines.append(
                    f"   - {batch['batch_code']:<11} {batch['status']:<10} "
                    f"bottles={batch['bottle_count']} published={batch['published_count']} "
                    f"pending={batch['pending_count']}"
                )

            lines.append("")
            lines.append("3. Query: published salinity values")
            samples = service.query_samples(voyage=VOYAGE, limit=6)
            for item in samples["items"]:
                overall = item["confidence"]["overall"]
                lines.append(
                    f"   - {item['batch']:<11} {item['label']:<9} {item['salinity']:>7} "
                    f"PSU  confidence={overall:.3f}  source="
                    f"{item['provenance']['handover'].get('file')}:"
                    f"{item['provenance']['handover'].get('line')}"
                )
            lines.append(f"   ({samples['total']} published rows in total)")

            lines.append("")
            lines.append("4. Review queue")
            queue = service.list_review_queue(voyage=VOYAGE)
            for item in queue["items"]:
                if item["kind"] == "low_confidence_extraction":
                    continue
                lines.append(f"   - [{item['batch_code']}] {item['kind']}: {item['title']}")
            extraction = [i for i in queue["items"] if i["kind"] == "low_confidence_extraction"]
            lines.append(f"   - {len(extraction)} low-confidence extraction rows awaiting a check")

            lines.append("")
            lines.append("5. Reviewer confirms one ambiguous bottle")
            item = next(i for i in queue["items"] if i["kind"] == "ambiguous_assignment")
            chosen = item["candidates"][0]["result"]
            service.decide(item["id"], "confirm", reviewer="张敏",
                           value={"result": chosen})
            batch = service.get_batch("KX-2603-G")
            lines.append(
                f"   - {batch['batch_code']} is now {batch['status']} "
                f"({batch['published_count']} rows published, all reviewer-confirmed)"
            )
            after = service.query_samples(batch="KX-2603-G")
            lines.append(f"   - query for {batch['batch_code']} returns {after['total']} rows")

            lines.append("")
            lines.append("6. Re-import the same files")
            again = service.import_materials(materials)
            lines.append(
                "   - " + ", ".join(f"{r.name}:{r.status}" for r in again)
            )
        finally:
            service.db.close()
    lines.extend(demo_paged())
    return "\n".join(lines)


def demo_paged() -> list[str]:
    """Offline walk-through: out-of-order page upload with a re-sent page."""
    fixtures = ROOT / "fixtures" / "paged_deck"
    voyage = "PG-26-01"
    lines: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        service = SamplingService(settings=Settings(
            database_url=f"sqlite:///{Path(tmp) / 'paged.db'}", fixture_dir=fixtures,
        ))
        try:
            service.import_materials([
                MaterialInput(name="handover.html",
                              content=(fixtures / "handover.html").read_text("utf-8"),
                              kind="handover", voyage=voyage),
                MaterialInput(name="lab_results.txt",
                              content=(fixtures / "lab_results.txt").read_text("utf-8"),
                              kind="lab", voyage=voyage),
            ])

            def page(number: int) -> PageInput:
                return PageInput(page_no=number, name=f"deck_p{number}.txt",
                                 content=(fixtures / f"deck_p{number}.txt").read_text("utf-8"))

            def upload(*numbers: int):
                return service.import_pages([MaterialInput(
                    name="deck", content="", kind="deck", voyage=voyage, slug="deck",
                    pages=[page(n) for n in numbers], version=1, expected_pages=4,
                )])[0]

            lines.append("")
            lines.append("7. Long deck table uploaded page by page, out of order")
            for label, numbers in (("page 3", (3,)), ("page 1", (1,)),
                                   ("page 1 again", (1,)), ("page 4", (4,)),
                                   ("page 2", (2,))):
                report = upload(*numbers)
                lines.append(
                    f"   - send {label:<12} status={report.status:<10} "
                    f"have={report.received_pages} missing={report.missing_pages}"
                )
                if report.status == "activated":
                    state = {b["batch_code"]: b["status"] for b in
                             service.list_batches(voyage=voyage)}
                    lines.append(f"     switched to version {report.version}: {state}")

            lines.append("")
            lines.append("8. Evidence still points at the original page file")
            for item in service.query_samples(voyage=voyage, batch="PG-2601-A")["items"][:2]:
                deck = item["provenance"]["deck"][0]
                lines.append(
                    f"   - {item['label']:<9} {item['salinity']:>7} PSU  "
                    f"{deck['file']}:{deck['line']} (page {deck['page']})"
                )
        finally:
            service.db.close()
    return lines


def main() -> int:
    started = time.time()
    print("=" * 72)
    print("Sampling importer — verification")
    print("=" * 72)
    print()
    print("Running automated tests (embedded SQLite, no network)...")
    ok, output = run_tests()
    print(output.rstrip())
    print()
    print("=" * 72)
    print("Fixture walk-through")
    print("=" * 72)
    print(service_summary := demo())
    print()
    elapsed = time.time() - started
    print("=" * 72)
    print(f"Tests: {'PASS' if ok else 'FAIL'}   elapsed: {elapsed:.1f}s")
    print("=" * 72)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
