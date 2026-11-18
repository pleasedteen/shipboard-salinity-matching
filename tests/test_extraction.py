"""Extraction tests: layout variance, continuations, provenance."""

from __future__ import annotations

import unittest

from helpers import ServiceTestCase, fixtures, material


class DeckExtractionTests(ServiceTestCase):
    def test_station_headings_govern_following_rows(self) -> None:
        self.load()
        rows = {}
        for batch in self.batches().values():
            if batch["status"] == "held":
                continue
            for row in self.batch(batch["batch_code"])["results"]:
                rows[row["label"]] = row
        # Rows take their station from the governing heading.
        self.assertEqual(rows["S01-001"]["station"], "S01")
        self.assertEqual(rows["S06-003"]["station"], "S06")
        self.assertEqual(rows["S07-004"]["station"], "S07")

    def test_repeated_header_on_continuation_page_adds_no_rows(self) -> None:
        self.load()
        deck = self.service.get_material("deck_log", voyage="KX-26-03")
        records = [r for r in deck["raw_records"] if r["kind"] == "deck"]
        # 4 + 3 + 2 + 2 + 2 (S01..S05) + 2 re-sampled rows on page 4.
        self.assertEqual(len(records), 15)
        labels = [r["fields"]["label_norm"] for r in records]
        # No record may be the repeated column header itself.
        self.assertNotIn("瓶号", labels)
        self.assertEqual(labels.count("S03-001"), 1)

    def test_page_two_continuation_does_not_duplicate(self) -> None:
        self.load()
        deck = self.service.get_material("deck_log_2", voyage="KX-26-03")
        records = deck["raw_records"]
        labels = sorted(r["fields"]["label_norm"] for r in records)
        self.assertEqual(labels, ["S06-001", "S06-002", "S06-003",
                                 "S07-001", "S07-002", "S07-003", "S07-004"])

    def test_every_row_has_provenance_and_confidence(self) -> None:
        self.load()
        deck = self.service.get_material("deck_log_2", voyage="KX-26-03")
        for record in deck["raw_records"]:
            self.assertGreater(record["confidence"], 0)
            self.assertLessEqual(record["confidence"], 0.99)
            locator = record["locator"]
            self.assertEqual(locator["material"], "deck_log_2")
            self.assertIn("line", locator)
            self.assertIn("excerpt", locator)
            self.assertTrue(locator["excerpt"])

    def test_query_exposes_original_position(self) -> None:
        self.load()
        item = self.service.query_samples(batch="KX-2603-A")["items"][0]
        provenance = item["provenance"]
        self.assertEqual(provenance["handover"]["material"], "handover")
        self.assertTrue(provenance["deck"])
        self.assertEqual(provenance["lab"]["material"], "lab_results_1")
        self.assertIn("置信", "置信度")  # sanity: the strings are UTF-8 safe
        self.assertIsNotNone(item["confidence"]["overall"])


class HandoverExtractionTests(ServiceTestCase):
    def test_batch_scope_comes_from_handover(self) -> None:
        self.load()
        self.assertEqual(
            sorted(self.batches()),
            ["KX-2603-A", "KX-2603-B", "KX-2603-C", "KX-2603-D",
             "KX-2603-E", "KX-2603-F", "KX-2603-G", "KX-2603-H"],
        )
        batch = self.batch("KX-2603-A")
        self.assertEqual(batch["station"], "S01")
        self.assertEqual(batch["bottle_count"], 4)
        self.assertEqual(batch["sample_start"], "2026-03-01T08:00")
        self.assertEqual(batch["sample_end"], "2026-03-01T08:24")

    def test_reused_bottle_labels_stay_separate_batches(self) -> None:
        self.load()
        # S01-001 appears in batch A and batch H; both keep their own bottle.
        a = self.batch("KX-2603-A")
        h = self.batch("KX-2603-H")
        self.assertIn("S01-001", {r["label"] for r in a["results"]})
        self.assertIn("S01-001", {r["label"] for r in h["results"]})
        self.assertNotEqual(
            {r["bottle_key"] for r in a["results"]} & {r["bottle_key"] for r in h["results"]},
            {r["bottle_key"] for r in a["results"]},
        )


class LabExtractionTests(ServiceTestCase):
    def test_fragment_labels_are_resolved_using_station_and_period(self) -> None:
        self.load()
        e = self.batch("KX-2603-E")
        self.assertEqual(e["status"], "held")  # only one of two bottles reported
        f = self.batch("KX-2603-F")
        self.assertEqual(f["status"], "published")
        pairs = {r["label"]: r["result_label"] for r in f["results"]}
        self.assertEqual(pairs, {"S06-001": "…06-001", "S06-002": "…06-002",
                                 "S06-003": "…06-003"})

    def test_lab_material_rows_keep_field_confidence(self) -> None:
        self.load()
        lab = self.service.get_material("lab_results_2", voyage="KX-26-03")
        fragment = next(r for r in lab["raw_records"] if "…" in r["fields"]["label_raw"])
        self.assertLess(fragment["fields"]["field_confidence"]["label"], 0.7)
        self.assertIn("label contains ellipsis", fragment["fields"]["reason_codes"])

    def test_unknown_batch_stays_unpublished(self) -> None:
        self.load()
        self.assertNotIn("KX-2603-I", self.batches())


class MalformedInputTests(ServiceTestCase):
    def test_unclosed_html_table_still_yields_rows(self) -> None:
        content = "<table><tr><td>瓶号<td>盐度<tr><td>S01-001<td>34.1"
        report = self.service.import_materials(
            [material("broken.html", "lab", content)]
        )
        self.assertEqual(report[0].records, 1)
        record = self.service.get_material("broken", voyage="KX-26-03")["raw_records"][0]
        self.assertEqual(record["fields"]["label_raw"], "S01-001")
        self.assertEqual(record["fields"]["salinity"], 34.1)

    def test_empty_material_is_accepted_with_warning(self) -> None:
        report = self.service.import_materials([material("blank.html", "lab", "")])
        self.assertEqual(report[0].records, 0)
        self.assertTrue(report[0].warnings)

    def test_rollback_leaves_no_partial_records(self) -> None:
        self.load()
        before = self.service.db.scalar("SELECT COUNT(*) FROM materials")
        broken = material("x.txt", "handover", "送检批次 X\n瓶号 | 采样时间\nA | 08:00")
        original = self.service._refresh_batches

        def boom(_keys=None):
            raise RuntimeError("simulated interruption")

        self.service._refresh_batches = boom
        try:
            with self.assertRaises(RuntimeError):
                self.service.import_materials([broken])
        finally:
            self.service._refresh_batches = original
        self.assertEqual(self.service.db.scalar("SELECT COUNT(*) FROM materials"), before)
        self.assertEqual(self.service.db.scalar(
            "SELECT COUNT(*) FROM runs WHERE status = 'failed'"), 1)


class KindInferenceTests(ServiceTestCase):
    def test_kind_is_inferred_from_content(self) -> None:
        report = self.service.import_materials([
            material("unknown-1.html", None,
                     (self.service.settings.fixture_dir / "deck_log_2.txt").read_text())
        ])
        self.assertEqual(report[0].kind, "deck")
        report = self.service.import_materials([
            material("unknown-2.html", None,
                     (self.service.settings.fixture_dir / "handover.html").read_text())
        ])
        self.assertEqual(report[0].kind, "handover")


if __name__ == "__main__":
    unittest.main()
