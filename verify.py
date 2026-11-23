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
        finally:
            service.db.close()
    return "\n".join(lines)


def legacy_demo() -> str:
    """Walk through a locally-built old library: quarantine → verify → restart."""
    from legacy import build_legacy_library, local_import, material_rows

    lines: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "legacy.db"
        # The "old library" fixture: the same materials imported by the old
        # build, with a server-side path recorded for every version.
        build_legacy_library(db_path)
        settings = Settings(database_url=f"sqlite:///{db_path}",
                            fixture_dir=ROOT / "fixtures")
        service = SamplingService(settings=settings)
        try:
            trust = {r["trust"] for r in material_rows(db_path)}
            lines.append("1. Opened the legacy library")
            lines.append(f"   - {len(material_rows(db_path))} path-bearing versions, "
                         f"trust={','.join(sorted(trust))}")

            statuses = {b["batch_code"]: b["status"] for b in service.list_batches()}
            lines.append(f"   - batches: {', '.join(f'{k}:{v}' for k, v in statuses.items())}")
            assert set(statuses.values()) == {"held"}, statuses
            assert service.query_samples(voyage=VOYAGE)["total"] == 0

            detail = service.get_material("handover", voyage=VOYAGE)
            lines.append("")
            lines.append("2. Remote (HTTP) view of a quarantined version")
            lines.append(f"   - content_available={detail['content_available']} "
                         f"quarantined={detail['quarantined']} "
                         f"raw_records={len(detail['raw_records'])}")
            assert not detail["content_available"]
            assert detail["raw_records"] == []

            lines.append("")
            lines.append("3. Operator re-sends the exact bytes from the local CLI")
            reports = local_import(service)
            lines.append("   - " + ", ".join(
                f"{r.slug}:{r.status}" for r in reports))
            assert all(r.verified and r.trust == "trusted" for r in reports)
            statuses = {b["batch_code"]: b["status"] for b in service.list_batches()}
            lines.append(f"   - batches: {', '.join(f'{k}:{v}' for k, v in statuses.items())}")
            assert statuses["KX-2603-A"] == "published"
            assert statuses["KX-2603-E"] == "held"
            assert statuses["KX-2603-G"] == "in_review"
            samples = service.query_samples(voyage=VOYAGE)["total"]
            lines.append(f"   - {samples} published rows now queryable")
            assert samples == 18
            verified = service.get_material("handover", voyage=VOYAGE)
            assert verified["content_available"]

            # A restart must reopen the same verified state without re-quarantining.
            snapshots = {b["batch_code"]: (b["status"], b["version"])
                         for b in service.list_batches()}
        finally:
            service.db.close()

        reopened = SamplingService(settings=settings)
        try:
            lines.append("")
            lines.append("4. Reopened the database")
            again = {b["batch_code"]: (b["status"], b["version"])
                     for b in reopened.list_batches()}
            lines.append(f"   - batch state unchanged: {again == snapshots}")
            assert again == snapshots
            assert {r["trust"] for r in material_rows(db_path)
                    if r["status"] == "active"} == {"trusted"}
            assert reopened.query_samples(voyage=VOYAGE)["total"] == 18
        finally:
            reopened.db.close()
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
    print("=" * 72)
    print("Legacy library walk-through (quarantine → local verification → restart)")
    print("=" * 72)
    try:
        print(legacy_demo())
    except AssertionError as exc:
        ok = False
        print(f"legacy walk-through FAILED: {exc!r}")
    print()
    elapsed = time.time() - started
    print("=" * 72)
    print(f"Tests: {'PASS' if ok else 'FAIL'}   elapsed: {elapsed:.1f}s")
    print("=" * 72)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
