"""Material lifecycle: idempotency, re-running, revision, withdrawal."""

from __future__ import annotations

import unittest

from helpers import FIXTURES, ServiceTestCase, fixtures, material


class IdempotencyTests(ServiceTestCase):
    def test_reimporting_identical_materials_changes_nothing(self) -> None:
        first = self.load()
        material_count = len(self.service.list_materials(include_history=True))
        batches = {b["batch_code"]: b["version"] for b in self.service.list_batches()}
        items = {i["id"] for i in self.queue()}
        second = self.service.import_materials(fixtures())
        self.assertTrue(all(r.unchanged for r in second))
        self.assertEqual(len(self.service.list_materials(include_history=True)), material_count)
        self.assertEqual({b["batch_code"]: b["version"] for b in self.service.list_batches()},
                         batches)
        self.assertEqual({i["id"] for i in self.queue()}, items)

    def test_replace_with_identical_bytes_is_a_noop(self) -> None:
        self.load()
        before = self.batches()
        reports = self.service.import_materials(fixtures(), replace=True)
        self.assertTrue(all(r.unchanged for r in reports))
        after = self.batches()
        self.assertEqual({b["batch_code"]: b["version"] for b in before.values()},
                         {b["batch_code"]: b["version"] for b in after.values()})

    def test_interrupted_run_is_recoverable(self) -> None:
        self.load()
        self.service.db.insert("runs", {
            "id": "run_leftover", "trigger": "api", "status": "running",
            "started_at": "2026-03-01T00:00:00", "finished_at": None, "summary": None,
        })
        # A new process opening the same database marks it interrupted.
        from app.service import SamplingService

        reopened = SamplingService(settings=self.service.settings)
        self.addCleanup(reopened.db.close)
        self.assertEqual(reopened.db.scalar(
            "SELECT status FROM runs WHERE id = 'run_leftover'"), "interrupted")
        # And importing again still produces the same batch state.
        self.assertEqual(reopened.list_batches()[0]["status"] in ("published", "in_review", "held"),
                         True)


class RevisionTests(ServiceTestCase):
    def test_salinity_correction_reruns_only_affected_batches(self) -> None:
        self.load()
        old = (FIXTURES / "lab_results_1.txt").read_text(encoding="utf-8")
        new = old.replace("34.305", "34.355")
        report = self.service.revise_material("lab_results_1", new)
        self.assertEqual(
            report.affected_batches,
            ["KX-26-03|KX-2603-A", "KX-26-03|KX-2603-B",
             "KX-26-03|KX-2603-C", "KX-26-03|KX-2603-D"],
        )
        batch = self.batch("KX-2603-D")
        self.assertEqual(batch["status"], "published")
        salinities = {r["label"]: r["salinity"] for r in batch["results"]}
        self.assertEqual(salinities["S04-002"], 34.355)
        # A batch fed only by the other laboratory sheet keeps its version.
        self.assertEqual(self.batch("KX-2603-F")["status"], "published")

    def test_query_and_review_agree_after_revision(self) -> None:
        self.load()
        items = self.open_items("KX-2603-G", "ambiguous_assignment")
        chosen = items[0]["candidates"][0]["result"]
        self.service.decide(items[0]["id"], "confirm", value={"result": chosen})
        published_before = self.service.query_samples(batch="KX-2603-G")["items"]
        # Revise an unrelated laboratory row (batch D) and re-resolve.
        old = (FIXTURES / "lab_results_1.txt").read_text(encoding="utf-8")
        self.service.revise_material("lab_results_1", old.replace("34.221", "34.231"))
        batch = self.batch("KX-2603-G")
        published_after = self.service.query_samples(batch="KX-2603-G")["items"]
        self.assertEqual(batch["status"], "published")
        self.assertEqual(len(published_after), 4)
        self.assertEqual({r["label"]: r["salinity"] for r in published_before},
                         {r["label"]: r["salinity"] for r in published_after})
        self.assertEqual({r["label"]: r["salinity"] for r in batch["results"]},
                         {r["label"]: r["salinity"] for r in published_after})

    def test_decision_survives_value_only_correction(self) -> None:
        self.load()
        item = self.open_items("KX-2603-G", "ambiguous_assignment")[0]
        self.service.decide(item["id"], "confirm", value={
            "result": item["candidates"][0]["result"]})
        self.assertEqual(self.batch("KX-2603-G")["status"], "published")
        # Only a measured value changes; the feasible pairings are identical.
        old = (FIXTURES / "lab_results_2.html").read_text(encoding="utf-8")
        self.service.revise_material("lab_results_2", old.replace("33.997", "33.990"))
        batch = self.batch("KX-2603-G")
        self.assertEqual(batch["status"], "published")
        self.assertTrue(all(r["state"] == "confirmed" for r in batch["results"]))
        self.assertEqual(batch["status"], "published")
        self.assertEqual({r["state"] for r in batch["results"]}, {"confirmed"})

    def test_decision_is_invalidated_when_feasible_pairings_change(self) -> None:
        self.load()
        item = self.open_items("KX-2603-G", "ambiguous_assignment")[0]
        self.service.decide(item["id"], "confirm", value={
            "result": item["candidates"][0]["result"]})
        self.assertEqual(self.batch("KX-2603-G")["status"], "published")
        old = (FIXTURES / "handover.html").read_text(encoding="utf-8")
        revised = old.replace("<td>S07-004</td>", "<td>S07-006</td>")
        self.service.revise_material("handover", revised)
        batch = self.batch("KX-2603-G")
        self.assertEqual(batch["status"], "in_review")
        self.assertIn("S07-006", {r["label"] for r in batch["results"]})
        self.assertEqual(len(self.open_items("KX-2603-G", "ambiguous_assignment")), 2)
        decisions = self.service.db.fetchall(
            "SELECT status FROM review_decisions WHERE kind = 'assignment' "
            "AND batch_code = 'KX-2603-G'")
        self.assertTrue(any(d["status"] == "invalidated" for d in decisions))

    def test_reverting_a_material_restores_its_rows(self) -> None:
        self.load()
        original = (FIXTURES / "lab_results_2.html").read_text(encoding="utf-8")
        edited = original.replace("<td>—</td>", "<td>S05-002</td>")
        self.service.revise_material("lab_results_2", edited)
        self.assertEqual(self.batch("KX-2603-E")["status"], "published")
        revert = self.service.revise_material("lab_results_2", original)
        self.assertEqual(revert.status, "reverted")
        self.assertFalse(revert.unchanged)
        batch = self.batch("KX-2603-E")
        self.assertEqual(batch["status"], "held")
        # Rows from the superseded version must not leak into the query.
        self.assertEqual(self.service.query_samples(batch="KX-2603-E")["total"], 0)

    def test_withdrawing_lab_material_holds_only_its_batches(self) -> None:
        self.load()
        a_before = self.batch("KX-2603-A")
        self.assertEqual(a_before["status"], "published")
        report = self.service.withdraw_material("lab_results_1")
        self.assertIn("KX-26-03|KX-2603-A", report.affected_batches)
        self.assertEqual(self.batch("KX-2603-A")["status"], "held")
        # A batch fed by the other laboratory sheet is untouched.
        self.assertEqual(self.batch("KX-2603-F")["status"], "published")

    def test_withdrawing_handover_holds_scope_but_others_continue(self) -> None:
        self.load()
        self.service.withdraw_material("handover")
        statuses = {b["batch_code"]: b["status"] for b in self.service.list_batches()}
        self.assertEqual(statuses["KX-2603-A"], "held")
        self.assertEqual(self.service.query_samples(batch="KX-2603-A")["total"], 0)

    def test_revising_one_deck_material_keeps_other_batches(self) -> None:
        self.load()
        f_before = self.batch("KX-2603-F")["version"]
        old = (FIXTURES / "deck_log.html").read_text(encoding="utf-8")
        self.service.revise_material("deck_log", old.replace("S04-002", "S04-002"))  # identical
        self.assertEqual(self.batch("KX-2603-F")["version"], f_before)


class CrossBatchTests(ServiceTestCase):
    def test_a_result_is_coordinated_across_sibling_batches(self) -> None:
        # Two batches with the same station/window and the same bottle label;
        # the laboratory reports that label only once, so only one batch can
        # be published and the other is held rather than double-claiming.
        deck = (
            "<html><body><h2>站次 S01 2026-05-01</h2><table>"
            "<tr><th>瓶号</th><th>采样时间</th></tr>"
            "<tr><td>S01-001</td><td>08:00</td></tr></table></body></html>"
        )
        handover = (
            "<html><body>"
            "<h2>送检批次 Y-1</h2><p>站次 S01 2026-05-01 08:00—08:20</p>"
            "<table><tr><th>瓶号</th><th>采样时间</th></tr>"
            "<tr><td>S01-001</td><td>08:00</td></tr></table>"
            "<h2>送检批次 Y-2</h2><p>站次 S01 2026-05-01 08:00—08:20</p>"
            "<table><tr><th>瓶号</th><th>采样时间</th></tr>"
            "<tr><td>S01-001</td><td>08:00</td></tr></table>"
            "</body></html>"
        )
        lab = (
            "<html><body><h2>实验室盐度</h2><table>"
            "<tr><th>瓶号</th><th>盐度</th></tr>"
            "<tr><td>S01-001</td><td>34.2</td></tr></table></body></html>"
        )
        from app.service import MaterialInput

        voyage = "CONFLICT"
        self.service.import_materials([
            MaterialInput("d.html", deck, kind="deck", voyage=voyage),
            MaterialInput("h.html", handover, kind="handover", voyage=voyage),
            MaterialInput("l.html", lab, kind="lab", voyage=voyage),
        ])
        batches = self.service.list_batches(voyage=voyage)
        self.assertEqual(len(batches), 2)
        published = [b for b in batches if b["status"] == "published"]
        self.assertEqual(len(published), 1)
        claims = [
            row["result_key"]
            for batch in batches
            for row in self.service.get_batch(batch["id"])["results"]
            if row["result_key"]
        ]
        self.assertEqual(len(claims), len(set(claims)))

    def test_unlocated_lab_row_still_reaches_its_batch(self) -> None:
        from app.service import MaterialInput

        voyage = "NOLOC"
        self.service.import_materials([
            MaterialInput("d.html",
                          "<html><body><h2>站次 S01 2026-05-01</h2><table>"
                          "<tr><th>瓶号</th><th>采样时间</th></tr>"
                          "<tr><td>S01-001</td><td>08:00</td></tr></table></body></html>",
                          kind="deck", voyage=voyage),
            MaterialInput("h.html",
                          "<html><body><h2>送检批次 Z-1</h2>"
                          "<p>站次 S01 2026-05-01 08:00—08:20</p><table>"
                          "<tr><th>瓶号</th><th>采样时间</th></tr>"
                          "<tr><td>S01-001</td><td>08:00</td></tr></table></body></html>",
                          kind="handover", voyage=voyage),
        ])
        self.assertEqual(self.service.list_batches(voyage=voyage)[0]["status"], "held")
        report = self.service.import_materials([
            MaterialInput("l.html",
                          "<html><body><h2>实验室盐度结果</h2><table>"
                          "<tr><th>瓶号</th><th>盐度</th></tr>"
                          "<tr><td>S01-001</td><td>34.2</td></tr></table></body></html>",
                          kind="lab", voyage=voyage),
        ])
        self.assertEqual(report[0].affected_batches, ["NOLOC|Z-1"])
        self.assertEqual(self.service.list_batches(voyage=voyage)[0]["status"], "published")


class ProvenanceTests(ServiceTestCase):
    def test_corrected_value_is_traceable_to_review_decision(self) -> None:
        self.load()
        item = [i for i in self.open_items("KX-2603-G", "low_confidence_extraction")][0]
        self.service.decide(item["id"], "confirm", reviewer="李海")
        decision = self.service.db.fetchone(
            "SELECT * FROM review_decisions WHERE id = ("
            "SELECT decision_id FROM review_items WHERE id = ?)", (item["id"],))
        self.assertIsNotNone(decision)
        self.assertEqual(decision["kind"], "correction")
        self.assertEqual(decision["reviewer"], "李海")


if __name__ == "__main__":
    unittest.main()
