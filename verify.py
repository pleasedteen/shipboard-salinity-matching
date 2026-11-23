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

            lines.append("")
            lines.append("7. Voyage daily report (by station and sampling day)")
            report = service.daily_report(voyage=VOYAGE)
            for item in report["items"]:
                median = item["salinity_median"]
                lines.append(
                    f"   - {item['station']:<4} {item['sample_day']}  "
                    f"bottles={item['bottle_count']} published={item['published_count']} "
                    f"median={'—' if median is None else f'{median:.4f}'}"
                )
            lines.append(
                f"   ({report['bottle_count']} registered bottles, "
                f"{report['published_count']} published; "
                f"drift vs samples: {len(service.daily_report_drift(VOYAGE))})"
            )
        finally:
            service.db.close()
    return "\n".join(lines)


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
