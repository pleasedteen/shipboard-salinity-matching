"""Two voyages may hold same-named reports and identical extracted rows.

Every artefact (materials, laboratory rows, batches, review items, decisions
and provenance) must stay independent per voyage from import through revision
and withdrawal, and every read surface must show the voyage's own current
evidence.
"""

from __future__ import annotations

import unittest

from helpers import FIXTURES, ServiceTestCase, fixtures

from app.service import Conflict, MaterialInput


VOYAGE_A = "KX-27-01"
VOYAGE_B = "KX-27-02"


def copied(materials, voyage: str) -> list[MaterialInput]:
    return [
        MaterialInput(m.name, m.content, kind=m.kind, voyage=voyage)
        for m in materials
    ]


class VoyageIsolationTests(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.service.import_materials(copied(fixtures(), VOYAGE_A))
        self.service.import_materials(copied(fixtures(), VOYAGE_B))

    def _batch(self, voyage: str, code: str) -> dict:
        row = self.service.db.fetchone(
            "SELECT id FROM batches WHERE voyage = ? AND batch_code = ?", (voyage, code)
        )
        return self.service.get_batch(row["id"])

    def test_same_named_reports_keep_independent_rows_and_batches(self) -> None:
        # The same 24 laboratory rows exist once per voyage instead of the
        # second import overwriting the first voyage's rows.
        self.assertEqual(
            self.service.db.scalar("SELECT COUNT(*) FROM lab_results", (), 0), 48
        )
        for voyage in (VOYAGE_A, VOYAGE_B):
            self.assertEqual(
                self.service.db.scalar(
                    "SELECT COUNT(*) FROM lab_results WHERE voyage = ?", (voyage,), 0
                ),
                24,
            )
            statuses = {b["batch_code"]: b["status"]
                        for b in self.service.list_batches(voyage=voyage)}
            self.assertEqual(statuses["KX-2603-A"], "published")
            self.assertEqual(statuses["KX-2603-E"], "held")
            self.assertEqual(statuses["KX-2603-G"], "in_review")

    def test_withdrawal_in_one_voyage_leaves_the_other_untouched(self) -> None:
        report = self.service.withdraw_material("lab_results_1", voyage=VOYAGE_A)
        self.assertTrue(report.withdrawn)
        self.assertEqual(self._batch(VOYAGE_A, "KX-2603-A")["status"], "held")
        # The sibling voyage keeps publishing its own identical report.
        self.assertEqual(self._batch(VOYAGE_B, "KX-2603-A")["status"], "published")
        active_a = self.service.db.scalar(
            "SELECT COUNT(*) FROM lab_results lr JOIN materials m ON m.id = lr.material_id "
            "WHERE m.slug = 'lab_results_1' AND m.voyage = ? AND lr.active = 1",
            (VOYAGE_A,), 0)
        active_b = self.service.db.scalar(
            "SELECT COUNT(*) FROM lab_results lr JOIN materials m ON m.id = lr.material_id "
            "WHERE m.slug = 'lab_results_1' AND m.voyage = ? AND lr.active = 1",
            (VOYAGE_B,), 0)
        self.assertEqual(active_a, 0)
        self.assertEqual(active_b, 11)

    def test_revision_in_one_voyage_does_not_change_the_other(self) -> None:
        original = (FIXTURES / "lab_results_1.txt").read_text("utf-8")
        self.service.revise_material(
            "lab_results_1", original.replace("34.305", "34.999"), voyage=VOYAGE_A
        )
        sal_a = {r["label"]: r["salinity"]
                 for r in self._batch(VOYAGE_A, "KX-2603-D")["results"]}
        sal_b = {r["label"]: r["salinity"]
                 for r in self._batch(VOYAGE_B, "KX-2603-D")["results"]}
        self.assertEqual(sal_a["S04-002"], 34.999)
        self.assertEqual(sal_b["S04-002"], 34.305)

    def test_correction_does_not_leak_between_voyages(self) -> None:
        item_a = next(
            i for i in self.service.list_review_queue(voyage=VOYAGE_A)["items"]
            if i["kind"] == "low_confidence_extraction"
        )
        self.service.decide(item_a["id"], "correct", reviewer="甲",
                            value={"fields": {"label_raw": "S07-003"}})
        decisions = self.service.db.fetchall(
            "SELECT voyage FROM review_decisions WHERE kind = 'correction' AND active = 1"
        )
        self.assertEqual([d["voyage"] for d in decisions], [VOYAGE_A])
        # Re-extracting voyage B's same-named material must not replay it.
        material_b = self.service._active_material(VOYAGE_B, "lab_results_2")
        self.service._extract(material_b["id"], VOYAGE_B, material_b["slug"],
                              material_b["name"], material_b["content"], "lab")
        row_b = self.service.db.fetchone(
            "SELECT payload FROM raw_records WHERE material_id = ? AND record_key = ?",
            (material_b["id"], item_a["record_key"]),
        )
        self.assertNotIn("S07-003", row_b["payload"])

    def test_review_detail_opens_the_items_own_voyage_material(self) -> None:
        for voyage in (VOYAGE_A, VOYAGE_B):
            item = next(
                i for i in self.service.list_review_queue(voyage=voyage)["items"]
                if i["kind"] == "ambiguous_assignment"
            )
            detail = self.service.get_review_item(item["id"])
            owner = self.service.db.fetchone(
                "SELECT voyage FROM materials WHERE id = ?",
                (detail["source"]["material_id"],),
            )
            self.assertEqual(owner["voyage"], voyage)
            self.assertEqual(detail["voyage"], voyage)

    def test_assignment_confirmation_is_scoped_to_one_voyage(self) -> None:
        item_a = next(
            i for i in self.service.list_review_queue(voyage=VOYAGE_A)["items"]
            if i["kind"] == "ambiguous_assignment"
        )
        self.service.decide(item_a["id"], "confirm", reviewer="甲",
                            value={"result": item_a["candidates"][0]["result"]})
        self.assertEqual(self._batch(VOYAGE_A, "KX-2603-G")["status"], "published")
        batch_b = self._batch(VOYAGE_B, "KX-2603-G")
        self.assertEqual(batch_b["status"], "in_review")
        self.assertTrue(all(r["state"] in ("auto", "pending") for r in batch_b["results"]))
        open_b = [
            i for i in self.service.list_review_queue(voyage=VOYAGE_B)["items"]
            if i["batch_code"] == "KX-2603-G" and i["kind"] == "ambiguous_assignment"
        ]
        self.assertEqual(len(open_b), 2)

    def test_review_queue_counts_are_voyage_scoped(self) -> None:
        counts_a = self.service.list_review_queue(voyage=VOYAGE_A)["counts"]
        counts_all = self.service.list_review_queue()["counts"]
        self.assertEqual(counts_all["open"], 2 * counts_a["open"])

    def test_query_shows_live_locator_and_confidence_after_revision(self) -> None:
        original = (FIXTURES / "lab_results_2.html").read_text("utf-8")
        # A layout-only change shifts the laboratory row's line number while
        # leaving every semantic fact (and therefore the feasible pairings)
        # untouched.
        revised = original.replace(
            "<h1>", "<p>实验室复测安排说明：结果按送检批次整理。</p>\n<h1>", 1
        )
        before = self.service.query_samples(voyage=VOYAGE_A, batch="KX-2603-F")["items"]
        self.service.revise_material("lab_results_2", revised, voyage=VOYAGE_A)
        after = self.service.query_samples(voyage=VOYAGE_A, batch="KX-2603-F")["items"]
        self.assertTrue(before and after)
        self.assertNotEqual(
            {i["provenance"]["lab"].get("line") for i in before},
            {i["provenance"]["lab"].get("line") for i in after},
        )
        for item in after:
            self.assertIsNotNone(item["confidence"]["overall"])
            self.assertIsNotNone(item["confidence"]["result"])
            self.assertTrue(item["provenance"]["lab"]["excerpt"])
        # The other voyage keeps the original, pre-revision location.
        other = self.service.query_samples(voyage=VOYAGE_B, batch="KX-2603-F")["items"]
        self.assertEqual(
            {i["provenance"]["lab"].get("line") for i in other},
            {i["provenance"]["lab"].get("line") for i in before},
        )

    def test_confirmed_decision_survives_equivalent_reorder_but_uses_new_provenance(self) -> None:
        item = next(
            i for i in self.service.list_review_queue(voyage=VOYAGE_A)["items"]
            if i["kind"] == "ambiguous_assignment"
        )
        self.service.decide(item["id"], "confirm", reviewer="甲",
                            value={"result": item["candidates"][0]["result"]})
        batch = self._batch(VOYAGE_A, "KX-2603-G")
        self.assertEqual(batch["status"], "published")

        original = (FIXTURES / "lab_results_2.html").read_text("utf-8")
        revised = original.replace(
            "<h1>", "<p>实验室复测安排说明：结果按送检批次整理。</p>\n<h1>", 1
        )
        self.service.revise_material("lab_results_2", revised, voyage=VOYAGE_A)
        batch = self._batch(VOYAGE_A, "KX-2603-G")
        self.assertEqual(batch["status"], "published")
        self.assertEqual({r["state"] for r in batch["results"]}, {"confirmed"})
        rows = self.service.query_samples(voyage=VOYAGE_A, batch="KX-2603-G")["items"]
        self.assertTrue(all(r["provenance"]["lab"]["line"] for r in rows))
        reopened = [
            i for i in self.service.list_review_queue(voyage=VOYAGE_A)["items"]
            if i["batch_code"] == "KX-2603-G" and i["kind"] == "ambiguous_assignment"
            and i["status"] == "open"
        ]
        self.assertEqual(reopened, [])

    def test_correction_from_a_stale_detail_is_refused_and_writes_nothing(self) -> None:
        item = next(
            i for i in self.service.list_review_queue(voyage=VOYAGE_A)["items"]
            if i["kind"] == "low_confidence_extraction"
        )
        original = (FIXTURES / "lab_results_2.html").read_text("utf-8")
        # Change the exact source row the open detail points at, so the
        # reviewer's snapshot no longer matches the current extraction.
        old_line = item["locator"]["excerpt"].split(" | ")[0]
        revised = original.replace(
            f"<td>{old_line}</td><td>S07-00…</td>",
            f"<td>{old_line}</td><td>S07-004</td>",
            1,
        )
        self.assertNotEqual(revised, original)
        self.service.revise_material("lab_results_2", revised, voyage=VOYAGE_A)
        with self.assertRaises(Conflict):
            self.service.decide(item["id"], "correct", reviewer="旧标签页",
                                value={"fields": {"label_raw": "S07-009"}})
        stray = self.service.db.fetchall(
            "SELECT * FROM review_decisions WHERE voyage = ? AND record_key = ?",
            (VOYAGE_A, item["record_key"]),
        )
        self.assertEqual(stray, [])

    def test_layout_only_revision_also_stales_an_open_detail(self) -> None:
        item = next(
            i for i in self.service.list_review_queue(voyage=VOYAGE_A)["items"]
            if i["kind"] == "low_confidence_extraction"
        )
        original = (FIXTURES / "lab_results_2.html").read_text("utf-8")
        # Same semantics, but the source row moves to a new line.
        revised = original.replace(
            "<h1>", "<p>实验室复测安排说明：结果按送检批次整理。</p>\n<h1>", 1
        )
        self.service.revise_material("lab_results_2", revised, voyage=VOYAGE_A)
        with self.assertRaises(Conflict):
            self.service.decide(item["id"], "correct", reviewer="旧标签页",
                                value={"fields": {"label_raw": "S07-009"}})

    def test_refreshed_detail_allows_the_correction_after_revision(self) -> None:
        item = next(
            i for i in self.service.list_review_queue(voyage=VOYAGE_A)["items"]
            if i["kind"] == "low_confidence_extraction"
        )
        original = (FIXTURES / "lab_results_2.html").read_text("utf-8")
        self.service.revise_material(
            "lab_results_2", original.replace("33.997", "33.991"), voyage=VOYAGE_A
        )
        # Refreshing the detail yields the current item, which can be decided.
        refreshed = self.service.get_review_item(item["id"])
        decided = self.service.decide(refreshed["id"], "confirm", reviewer="甲")
        self.assertEqual(decided["status"], "resolved")


class LegacySchemaMigrationTests(ServiceTestCase):
    def test_old_global_unique_lab_index_is_replaced(self) -> None:
        db = self.service.db
        db.execute("DROP INDEX idx_lab_key")
        db.execute("CREATE UNIQUE INDEX idx_lab_key ON lab_results(record_key)")
        db._run_migrations()
        # A second voyage can now store the same record_key without colliding.
        db.execute(
            "INSERT INTO lab_results (id, record_key, material_id, voyage, confidence, "
            "fingerprint, present, active, updated_at) VALUES "
            "('lab_old_a', 'shared_key', 'mat_x', 'VA', 0.9, 'fp', 1, 1, '2026-01-01T00:00')"
        )
        db.execute(
            "INSERT INTO lab_results (id, record_key, material_id, voyage, confidence, "
            "fingerprint, present, active, updated_at) VALUES "
            "('lab_old_b', 'shared_key', 'mat_y', 'VB', 0.9, 'fp', 1, 1, '2026-01-01T00:00')"
        )
        self.assertEqual(
            db.scalar(
                "SELECT COUNT(*) FROM lab_results WHERE record_key = 'shared_key'", (), 0
            ),
            2,
        )

    def test_review_items_gain_source_material_id_column(self) -> None:
        columns = {r["name"] for r in self.service.db._table_columns("review_items")}
        self.assertIn("source_material_id", columns)


if __name__ == "__main__":
    unittest.main()
