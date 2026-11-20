#!/usr/bin/env python3
"""Offline reproduction of the cross-voyage and revision scenarios.

Run with::

    python3 repro.py

It uses only the bundled fixtures and an in-memory-adjacent temporary SQLite
database, so nothing touches the network or a real deployment.

Two flows are walked through:

1. Two voyages import byte-identical materials under the same filenames.
   They must behave as separate materials end to end: withdrawing or deciding
   one voyage must not change the other voyage's query rows, provenance or
   review decisions, and re-importing identical bytes must add no records.

2. A material is revised (equivalent re-versioning), a low-confidence row is
   corrected, and then the material is withdrawn.  The correction and its
   review evidence must land on the *current* version's row, survive the
   equivalent revision, and re-importing the withdrawn bytes must restore the
   same material rather than create another record.

Exit code 0 means every invariant held.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.service import MaterialInput, SamplingService  # noqa: E402

FIXTURES = ROOT / "fixtures"
FILES = (
    ("deck_log.html", "deck"),
    ("deck_log_2.txt", "deck"),
    ("handover.html", "handover"),
    ("lab_results_1.txt", "lab"),
    ("lab_results_2.html", "lab"),
)


def materials(voyage: str) -> list[MaterialInput]:
    return [
        MaterialInput(name=name, content=(FIXTURES / name).read_text(encoding="utf-8"),
                      kind=kind, voyage=voyage)
        for name, kind in FILES
    ]


def snapshot(service: SamplingService, voyage: str) -> dict:
    """Everything a caller can observe about one voyage."""
    batches = {b["batch_code"]: (b["status"], b["version"])
               for b in service.list_batches(voyage=voyage)}
    samples = [
        (i["batch"], i["label"], i["salinity"], i["published"],
         i["provenance"]["handover"].get("line") if i["provenance"]["handover"] else None,
         i["provenance"]["lab"].get("line") if i["provenance"]["lab"] else None,
         tuple(d.get("line") for d in i["provenance"]["deck"]))
        for i in service.query_samples(voyage=voyage)["items"]
    ]
    review = sorted(
        (i["batch_code"], i["kind"], i["status"], i["detail"])
        for i in service.list_review_queue(status="open", voyage=voyage)["items"]
    )
    return {"batches": batches, "samples": samples, "review": review}


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"   ok  {message}")


def flow_cross_voyage(service: SamplingService) -> None:
    print("\n1. Two voyages, same filenames, same content")
    service.import_materials(materials("V1"))
    service.import_materials(materials("V2"))

    duplicates = service.db.fetchall(
        "SELECT record_key, COUNT(DISTINCT material_id) AS n FROM raw_records "
        "GROUP BY record_key HAVING n > 1")
    check(not duplicates, "no record key is shared between the two voyages")
    lab_duplicates = service.db.fetchall(
        "SELECT record_key, COUNT(*) AS n FROM lab_results "
        "GROUP BY record_key HAVING n > 1")
    check(not lab_duplicates, "no laboratory row is shared between the two voyages")

    v1 = snapshot(service, "V1")
    v2 = snapshot(service, "V2")
    check(v1["batches"] == v2["batches"], "both voyages resolve to the same batch states")
    check(v1["samples"] == v2["samples"], "both voyages return the same rows and provenance")

    count = service.db.scalar("SELECT COUNT(*) FROM materials")
    service.import_materials(materials("V1"))
    service.import_materials(materials("V2"))
    check(service.db.scalar("SELECT COUNT(*) FROM materials") == count,
          "re-importing identical bytes adds no material records")

    # Withdrawing V2's laboratory report must leave V1 untouched.
    service.withdraw_material("lab_results_1", voyage="V2")
    check(snapshot(service, "V1") == v1, "V1 is unchanged after withdrawing V2's lab report")
    check(snapshot(service, "V2") != v2, "V2 itself did change (its batches are held)")
    v2_after = snapshot(service, "V2")
    check(v2_after["batches"]["KX-2603-A"][0] == "held", "V2 batch A is held")

    # And the other way around: a decision in V1 is not inherited by V2.
    item = next(i for i in service.list_review_queue(voyage="V1")["items"]
                if i["kind"] == "ambiguous_assignment")
    service.decide(item["id"], "confirm", value={"result": item["candidates"][0]["result"]})
    check(service.query_samples(voyage="V1", batch="KX-2603-G")["total"] == 4,
          "V1's ambiguous batch becomes fully published")
    v2_items = [i for i in service.list_review_queue(voyage="V2")["items"]
                if i["kind"] == "ambiguous_assignment"]
    check(len(v2_items) == 2, "V2 still has its own two open ambiguous items")
    check(service.query_samples(voyage="V2", batch="KX-2603-G")["total"] == 2,
          "V2 publishes only its invariant pairs")


def flow_revise_correct_withdraw(service: SamplingService) -> None:
    print("\n2. Equivalent revision, correction, then withdrawal")
    voyage = "REV"
    service.import_materials(materials(voyage))
    original = (FIXTURES / "lab_results_2.html").read_text(encoding="utf-8")
    missing = next(
        i for i in service.list_review_queue(voyage=voyage)["items"]
        if i["batch_code"] == "KX-2603-E"
        and i["kind"] == "low_confidence_extraction"
        and i["payload"]["fields"].get("label_raw") == "—"
    )

    # Equivalent re-versioning: only layout changes, every row keeps its meaning.
    revision = original.replace("</table>", "</table>\n")
    service.revise_material("lab_results_2", revision, voyage=voyage)
    active_id = service.db.scalar(
        "SELECT id FROM materials WHERE slug = 'lab_results_2' "
        "AND voyage = ? AND status = 'active'", (voyage,))
    refreshed = next(
        i for i in service.list_review_queue(voyage=voyage)["items"]
        if i["record_key"] == missing["record_key"]
    )
    check(refreshed["payload"]["material_id"] == active_id,
          "review evidence points at the current material version")
    check(service.get_review_item(refreshed["id"])["source"]["material_id"] == active_id,
          "the source text shown to the reviewer is the current version")

    before = service.db.scalar("SELECT COUNT(*) FROM materials")
    service.decide(refreshed["id"], "correct", reviewer="张敏",
                   value={"fields": {"label_raw": "S05-002"}})
    check(service.db.scalar("SELECT COUNT(*) FROM materials") == before,
          "resolving a correction adds no material record")
    rows = service.db.fetchall(
        "SELECT r.payload, m.status FROM raw_records r "
        "JOIN materials m ON m.id = r.material_id WHERE r.record_key = ?",
        (missing["record_key"],))
    current = [r for r in rows if r["status"] == "active"]
    superseded = [r for r in rows if r["status"] == "superseded"]
    check(len(current) == 1 and "S05-002" in current[0]["payload"],
          "the correction lands on the current version's row")
    check(all("S05-002" not in r["payload"] for r in superseded),
          "the superseded row is left untouched")
    check(service.query_samples(voyage=voyage, batch="KX-2603-E")["total"] == 2,
          "the corrected batch becomes published")

    # Another equivalent revision + revert: the correction replays because the
    # row's meaning is unchanged.
    service.revise_material("lab_results_2", revision + "\n", voyage=voyage)
    check(service.query_samples(voyage=voyage, batch="KX-2603-E")["total"] == 2,
          "the correction still applies after a second equivalent revision")
    revert = service.revise_material("lab_results_2", original, voyage=voyage)
    check(revert.status == "reverted", "re-submitting earlier bytes is a revert, not a new record")
    check(service.query_samples(voyage=voyage, batch="KX-2603-E")["total"] == 2,
          "the correction replays after the revert")
    decision = service.db.fetchone(
        "SELECT status FROM review_decisions WHERE record_key = ? AND kind = 'correction'",
        (missing["record_key"],))
    check(decision["status"] == "active", "the correction decision is still active")

    # Withdraw, then re-import the same bytes: restore, don't duplicate.
    service.withdraw_material("lab_results_2", voyage=voyage)
    check(service.query_samples(voyage=voyage, batch="KX-2603-E")["total"] == 0,
          "withdrawal holds the batch")
    active_versions = service.db.scalar(
        "SELECT COUNT(*) FROM materials WHERE slug = 'lab_results_2' AND voyage = ?",
        (voyage,))
    restored = service.import_materials([
        m for m in materials(voyage) if m.name == "lab_results_2.html"
    ])[0]
    check(restored.status == "restored" and not restored.created,
          "re-importing the withdrawn bytes restores the same material")
    check(service.db.scalar(
        "SELECT COUNT(*) FROM materials WHERE slug = 'lab_results_2' AND voyage = ?",
        (voyage,)) == active_versions,
          "restoring adds no material record for this slug")
    check(service.query_samples(voyage=voyage, batch="KX-2603-E")["total"] == 2,
          "the restored material publishes the corrected batch again")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        service = SamplingService(settings=Settings(
            database_url=f"sqlite:///{Path(tmp) / 'repro.db'}", fixture_dir=FIXTURES))
        try:
            flow_cross_voyage(service)
            flow_revise_correct_withdraw(service)
        finally:
            service.db.close()
    print("\nAll invariants held.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
