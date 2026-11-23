"""Quarantine of legacy path imports, local verification, restart consistency.

A database written by the old build records a server-side path for every
material.  Those versions cannot be vouched for over HTTP, so opening the
library quarantines them: they no longer inform batches, queries or review,
and their bodies/extractions are withheld from remote callers.  Only the
local CLI, which reads the file itself, can re-send the exact bytes and verify
the original version again.
"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from helpers import FIXTURES, VOYAGE, ServiceTestCase
from legacy import build_legacy_library, local_import, material_rows

from app.config import Settings
from app.service import Conflict, SamplingService

ALL_SLUGS = ("deck_log", "deck_log_2", "handover", "lab_results_1", "lab_results_2")


class QuarantineTestCase(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        # A fresh "old library" file, then open it with the current build.
        self.service.db.close()
        build_legacy_library(self.db_path)
        self.service = SamplingService(settings=Settings(
            database_url=f"sqlite:///{self.db_path}", fixture_dir=FIXTURES,
        ))
        self.addCleanup(self.service.db.close)

    def reopen(self) -> SamplingService:
        reopened = SamplingService(settings=Settings(
            database_url=f"sqlite:///{self.db_path}", fixture_dir=FIXTURES,
        ))
        self.addCleanup(reopened.db.close)
        return reopened

    def rebuild(self, **kwargs) -> SamplingService:
        """Replace the database file with a fresh legacy library."""
        self.service.db.close()
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(self.db_path) + suffix)
            if path.exists():
                path.unlink()
        build_legacy_library(self.db_path, **kwargs)
        self.service = self.reopen()
        return self.service


class LegacyQuarantineTests(QuarantineTestCase):
    def test_path_bearing_versions_are_quarantined_on_open(self) -> None:
        rows = material_rows(self.db_path)
        self.assertTrue(rows)
        self.assertTrue(all(r["path"] for r in rows))
        self.assertEqual({r["trust"] for r in rows}, {"quarantined"})
        self.assertEqual({r["origin"] for r in rows}, {"legacy_path"})

    def test_quarantined_materials_stop_informing_batches(self) -> None:
        statuses = {b["batch_code"]: b["status"] for b in self.service.list_batches()}
        self.assertTrue(statuses)
        self.assertEqual(set(statuses.values()), {"held"})
        self.assertEqual(self.service.query_samples(voyage=VOYAGE)["total"], 0)
        for batch in self.service.list_batches():
            self.assertEqual(batch["published_count"], 0)
            self.assertEqual(batch["pending_count"], batch["bottle_count"])

    def test_quarantined_versions_do_not_expose_content(self) -> None:
        detail = self.service.get_material("handover", voyage=VOYAGE)
        self.assertFalse(detail["content_available"])
        self.assertTrue(detail["quarantined"])
        self.assertEqual(detail["raw_records"], [])
        # The local machine can inspect the original while verifying it.
        local = self.service.get_material("handover", voyage=VOYAGE, local=True)
        self.assertTrue(local["content_available"])
        self.assertTrue(local["raw_records"])

    def test_review_items_are_redacted_for_remote_callers(self) -> None:
        # Keep one handover-forwarding report trusted so a review item exists;
        # the quarantined lab report it displaces supplies that item's source.
        service = self.rebuild(path_slugs=("lab_results_2",))
        items = []
        for status in ("open", "stale", "closed"):
            items += service.list_review_queue(status=status, voyage=VOYAGE)["items"]
        redacted = [i for i in items if i["quarantined"]]
        self.assertTrue(redacted)
        for item in redacted:
            self.assertEqual(item["payload"], {})
            self.assertEqual(item["candidates"], [])
            self.assertEqual(item["locator"], {})
            detail = service.get_review_item(item["id"])
            if "source" in detail:
                self.assertFalse(detail["source"]["content_available"])
                self.assertNotIn("content", detail["source"])
        local_item = service.get_review_item(redacted[0]["id"], local=True)
        self.assertFalse(local_item["quarantined"])
        self.assertTrue(local_item["payload"])
        self.assertTrue(local_item["locator"])
        # Trusted materials keep their extracted content visible.
        trusted = [i for i in items if not i["quarantined"] and i["status"] == "closed"]
        self.assertTrue(trusted)

    def test_remote_decision_on_quarantined_item_is_refused(self) -> None:
        service = self.rebuild(path_slugs=("lab_results_2",))
        pool = []
        for status in ("open", "stale", "closed"):
            pool += service.list_review_queue(status=status, voyage=VOYAGE)["items"]
        item = next(i for i in pool if i["quarantined"])
        with self.assertRaises(Conflict):
            service.decide(item["id"], "acknowledge", reviewer="远端")
        self.assertEqual(
            service.list_review_queue(voyage=VOYAGE)["items"][0]["status"], "open")

    def test_batch_view_never_quotes_quarantined_material(self) -> None:
        # Only the deck logs and a lab report are unverified; batch A keeps its
        # verified handover, but the recomputed batch never consumes an
        # unverified row, so no provenance is attributed to one.
        service = self.rebuild(path_slugs=("deck_log", "deck_log_2", "lab_results_2"))
        batch = service.get_batch("KX-2603-A")
        self.assertEqual(batch["status"], "published")
        quoted = set()
        for item in batch["results"]:
            handover = item["provenance"].get("handover") or {}
            if handover.get("file"):
                quoted.add(handover["file"])
            for locator in item["provenance"].get("deck", []):
                if locator.get("file"):
                    quoted.add(locator["file"])
            lab = item["provenance"].get("lab") or {}
            if lab.get("file"):
                quoted.add(lab["file"])
        self.assertNotIn("deck_log.html", quoted)
        self.assertNotIn("deck_log_2.txt", quoted)
        self.assertNotIn("lab_results_2.html", quoted)

    def test_query_never_quotes_quarantined_material(self) -> None:
        # Regression: previously a re-resolution could keep stale provenance
        # that quoted a file now under quarantine.
        service = self.rebuild(path_slugs=("deck_log", "deck_log_2", "lab_results_2"))
        quarantined_files = {"deck_log.html", "deck_log_2.txt", "lab_results_2.html"}
        for item in service.query_samples(voyage=VOYAGE)["items"]:
            files = set()
            for key in ("deck", "lab"):
                value = item["provenance"].get(key)
                if isinstance(value, dict):
                    files.add(value.get("file"))
                elif isinstance(value, list):
                    files |= {loc.get("file") for loc in value}
            handover = item["provenance"].get("handover") or {}
            files.add(handover.get("file"))
            self.assertFalse(files & quarantined_files, item["label"])

    def test_body_import_can_supersede_a_quarantined_version(self) -> None:
        # A fresh body-only import in the same voyage replaces the quarantined
        # version; the new bytes are trusted and the legacy one stays preserved.
        from app.service import MaterialInput

        revised = (FIXTURES / "handover.html").read_text(encoding="utf-8") \
            .replace("<td>S07-004</td>", "<td>S07-006</td>")
        report = self.service.import_materials([MaterialInput(
            name="handover.html", content=revised, kind="handover", voyage=VOYAGE,
        )])[0]
        self.assertEqual(report.status, "created")
        self.assertEqual(report.trust, "trusted")
        rows = [r for r in material_rows(self.db_path) if r["slug"] == "handover"]
        legacy = next(r for r in rows if r["version"] == 1)
        self.assertEqual((legacy["status"], legacy["trust"]), ("superseded", "quarantined"))
        self.assertEqual(self.service.query_samples(voyage=VOYAGE)["total"], 0)
        # The legacy version is still locally inspectable for verification.
        self.assertEqual(legacy["path"], f"/srv/sampling/incoming/material.txt")

    def test_unaffected_body_only_material_keeps_being_imported(self) -> None:
        # A new body-only material in a new voyage is processed normally even
        # while the legacy voyage is quarantined.
        fresh = self.service.import_materials(_body_fixtures("FRESH"))
        self.assertTrue(all(r.created and r.trust == "trusted" for r in fresh))
        batches = {b["batch_code"]: b for b in self.service.list_batches(voyage="FRESH")}
        self.assertEqual(batches["KX-2603-A"]["status"], "published")
        self.assertEqual(self.service.query_samples(voyage="FRESH")["total"], 18)

    def test_body_resend_of_identical_legacy_bytes_stays_quarantined(self) -> None:
        report = self.service.import_materials([
            _body_material("handover", "handover.html")
        ])[0]
        self.assertEqual(report.status, "quarantined")
        self.assertEqual(report.trust, "quarantined")
        self.assertFalse(report.verified)


class LocalVerificationTests(QuarantineTestCase):
    def test_resending_exact_bytes_restores_the_original_version(self) -> None:
        before = {r["slug"]: r for r in material_rows(self.db_path) if r["status"] == "active"}
        mat_id = before["handover"]["id"]
        reports = local_import(self.service)
        by_slug = {r.slug: r for r in reports}
        self.assertTrue(all(r.verified for r in reports), by_slug)
        self.assertEqual({r.trust for r in reports}, {"trusted"})
        # The same version row is verified, not replaced by a new one.
        after = {r["slug"]: r for r in material_rows(self.db_path) if r["status"] == "active"}
        self.assertEqual(after["handover"]["id"], mat_id)
        self.assertEqual(after["handover"]["origin"], "local_verified")
        # Pairing is recomputed: batches go back to their expected states.
        statuses = {b["batch_code"]: b["status"] for b in self.service.list_batches()}
        self.assertEqual(statuses["KX-2603-A"], "published")
        self.assertEqual(statuses["KX-2603-E"], "held")
        self.assertEqual(statuses["KX-2603-G"], "in_review")
        self.assertEqual(self.service.query_samples(voyage=VOYAGE)["total"], 18)

    def test_verified_material_is_readable_again_over_http(self) -> None:
        local_import(self.service)
        detail = self.service.get_material("handover", voyage=VOYAGE)
        self.assertTrue(detail["content_available"])
        self.assertFalse(detail["quarantined"])
        self.assertNotIn("content", detail)
        local = self.service.get_material("handover", voyage=VOYAGE, local=True)
        self.assertIn("KX-2603-A", local["content"])
        item = next(i for i in self.service.list_review_queue(voyage=VOYAGE)["items"]
                    if i["kind"] == "ambiguous_assignment")
        self.assertFalse(item["quarantined"])
        self.assertTrue(item["candidates"])

    def test_restart_keeps_verified_state_and_results(self) -> None:
        local_import(self.service)
        expected = {b["batch_code"]: (b["status"], b["version"])
                    for b in self.service.list_batches()}
        samples = self.service.query_samples(voyage=VOYAGE)["total"]
        self.service.db.close()
        reopened = self.reopen()
        actual = {b["batch_code"]: (b["status"], b["version"])
                  for b in reopened.list_batches()}
        self.assertEqual(actual, expected)
        self.assertEqual(reopened.query_samples(voyage=VOYAGE)["total"], samples)
        self.assertEqual(
            {r["trust"] for r in material_rows(self.db_path) if r["status"] == "active"},
            {"trusted"},
        )


class HistoricalVersionTests(QuarantineTestCase):
    def test_replaced_version_keeps_history_after_local_verification(self) -> None:
        # A corrected laboratory report leaves version 1 superseded in the old
        # library; both versions carry the legacy path.
        service = self.rebuild(correct_lab_value=True, path_slugs=("lab_results_1",))
        versions = [r for r in material_rows(self.db_path) if r["slug"] == "lab_results_1"]
        self.assertEqual(len(versions), 2)
        superseded = next(r for r in versions if r["status"] == "superseded")
        active = next(r for r in versions if r["status"] == "active")
        self.assertEqual(superseded["trust"], "quarantined")

        # Re-send both byte-for-byte from the local CLI.
        original = (FIXTURES / "lab_results_1.txt").read_text(encoding="utf-8")
        corrected = original.replace("34.305", "34.355")
        service.import_materials([_local_material("lab_results_1.txt", original)])
        service.import_materials([_local_material("lab_results_1.txt", corrected)])

        after = {r["id"]: r for r in material_rows(self.db_path)}
        self.assertEqual(after[superseded["id"]]["status"], "superseded")
        self.assertEqual(after[superseded["id"]]["trust"], "trusted")
        self.assertEqual(after[superseded["id"]]["origin"], "local_verified")
        self.assertEqual(after[active["id"]]["status"], "active")
        self.assertEqual(after[active["id"]]["trust"], "trusted")
        batch = service.get_batch("KX-2603-D")
        self.assertEqual(batch["status"], "published")
        self.assertEqual({r["label"]: r["salinity"] for r in batch["results"]}["S04-002"],
                         34.355)

    def test_withdrawn_version_keeps_history_after_local_verification(self) -> None:
        service = self.rebuild(withdraw=("lab_results_2",), path_slugs=("lab_results_2",))
        withdrawn = next(r for r in material_rows(self.db_path)
                         if r["slug"] == "lab_results_2" and r["status"] == "withdrawn")
        self.assertEqual(withdrawn["trust"], "quarantined")
        content = (FIXTURES / "lab_results_2.html").read_text(encoding="utf-8")
        report = service.import_materials([_local_material("lab_results_2.html", content)])
        self.assertTrue(report[0].verified)
        after = {r["id"]: r for r in material_rows(self.db_path)}[withdrawn["id"]]
        self.assertEqual(after["status"], "withdrawn")
        self.assertEqual(after["trust"], "trusted")
        # The withdrawn report still does not feed any batch.
        self.assertEqual(service.get_batch("KX-2603-F")["status"], "held")


# -- helpers ----------------------------------------------------------------

def _body_material(slug: str, filename: str):
    from app.service import MaterialInput

    return MaterialInput(
        name=filename,
        content=(FIXTURES / filename).read_text(encoding="utf-8"),
        voyage=VOYAGE,
        slug=slug,
    )


def _body_fixtures(voyage: str):
    from app.service import MaterialInput

    return [
        MaterialInput(name=name, content=(FIXTURES / name).read_text(encoding="utf-8"),
                      kind=kind, voyage=voyage)
        for name, kind in (
            ("deck_log.html", "deck"), ("deck_log_2.txt", "deck"),
            ("handover.html", "handover"), ("lab_results_1.txt", "lab"),
            ("lab_results_2.html", "lab"),
        )
    ]


def _local_material(filename: str, content: str):
    from app.service import MaterialInput

    return MaterialInput(
        name=filename,
        content=content,
        voyage=VOYAGE,
        slug=Path(filename).stem,
        path=f"/srv/sampling/incoming/{filename}",
        local=True,
    )


if __name__ == "__main__":
    unittest.main()
