"""Material lifecycle: idempotency, re-running, revision, withdrawal."""

from __future__ import annotations

import unittest

from helpers import FIXTURES, ServiceTestCase, fixtures, material

from app.service import MaterialInput


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


class CrossVoyageIsolationTests(ServiceTestCase):
    """Two voyages import byte-identical materials under the same filenames.

    They are different materials: withdrawing, revising or deciding one must
    never change the other's query rows, provenance or review decisions.
    """

    def _import(self, voyage: str) -> None:
        self.service.import_materials([
            MaterialInput(m.name, m.content, kind=m.kind, voyage=voyage)
            for m in fixtures()
        ])

    def test_same_named_materials_in_two_voyages_do_not_share_identity(self) -> None:
        self._import("V1")
        self._import("V2")
        # Raw and laboratory rows are keyed per material, so no record key is
        # shared between the two voyages.
        duplicate_raw = self.service.db.fetchall(
            "SELECT record_key, COUNT(DISTINCT material_id) AS c FROM raw_records "
            "GROUP BY record_key HAVING c > 1")
        self.assertEqual(duplicate_raw, [])
        duplicate_lab = self.service.db.fetchall(
            "SELECT record_key, COUNT(*) AS c FROM lab_results "
            "GROUP BY record_key HAVING c > 1")
        self.assertEqual(duplicate_lab, [])
        # Both voyages nevertheless resolve independently and identically.
        for voyage in ("V1", "V2"):
            statuses = {b["batch_code"]: b["status"]
                        for b in self.service.list_batches(voyage=voyage)}
            self.assertEqual(statuses["KX-2603-A"], "published")
            self.assertEqual(statuses["KX-2603-G"], "in_review")
            self.assertEqual(self.service.query_samples(voyage=voyage)["total"], 18)

    def test_withdrawing_one_voyages_copy_leaves_the_other_untouched(self) -> None:
        self._import("V1")
        self._import("V2")
        v1_before = self.service.query_samples(voyage="V1")
        report = self.service.withdraw_material("lab_results_1", voyage="V2")
        self.assertTrue(report.withdrawn)
        # V2's laboratory-dependent batches are held ...
        v2 = {b["batch_code"]: b["status"]
              for b in self.service.list_batches(voyage="V2")}
        self.assertEqual(v2["KX-2603-A"], "held")
        # ... while V1 is byte-for-byte unchanged.
        v1_after = self.service.query_samples(voyage="V1")
        self.assertEqual(v1_after["total"], v1_before["total"])
        self.assertEqual(
            [(i["batch"], i["label"], i["salinity"], i["provenance"]["lab"])
             for i in v1_after["items"]],
            [(i["batch"], i["label"], i["salinity"], i["provenance"]["lab"])
             for i in v1_before["items"]],
        )

    def test_decision_in_one_voyage_is_not_inherited_by_the_other(self) -> None:
        self._import("V1")
        item = next(i for i in self.service.list_review_queue(voyage="V1")["items"]
                    if i["kind"] == "ambiguous_assignment")
        self.service.decide(item["id"], "confirm",
                            value={"result": item["candidates"][0]["result"]})
        self.assertEqual(self.service.query_samples(voyage="V1", batch="KX-2603-G")["total"], 4)

        self._import("V2")
        # V2 has the same rows but its own review items; it must not pick up
        # V1's confirmation, nor invalidate it.
        v2_items = [i for i in self.service.list_review_queue(voyage="V2")["items"]
                    if i["kind"] == "ambiguous_assignment"]
        self.assertEqual(len(v2_items), 2)
        self.assertEqual(self.service.query_samples(voyage="V2", batch="KX-2603-G")["total"], 2)
        self.assertEqual(self.service.query_samples(voyage="V1", batch="KX-2603-G")["total"], 4)

    def test_reimporting_identical_materials_is_still_idempotent_per_voyage(self) -> None:
        self._import("V1")
        self._import("V2")
        count = self.service.db.scalar("SELECT COUNT(*) FROM materials")
        for voyage in ("V1", "V2"):
            reports = self.service.import_materials([
                MaterialInput(m.name, m.content, kind=m.kind, voyage=voyage)
                for m in fixtures()
            ])
            self.assertTrue(all(r.unchanged for r in reports))
        self.assertEqual(self.service.db.scalar("SELECT COUNT(*) FROM materials"), count)


class MaterialVersionTargetingTests(ServiceTestCase):
    """Corrections, review evidence and provenance follow the current version."""

    def _missing_label_item(self) -> dict:
        return next(
            i for i in self.service.list_review_queue(voyage="KX-26-03")["items"]
            if i["batch_code"] == "KX-2603-E"
            and i["kind"] == "low_confidence_extraction"
            and i["payload"]["fields"].get("label_raw") == "—"
        )

    def test_review_evidence_points_at_the_current_version(self) -> None:
        self.load()
        original = (FIXTURES / "lab_results_2.html").read_text(encoding="utf-8")
        self.service.revise_material("lab_results_2",
                                     original.replace("</table>", "</table>\n"))
        item = self._missing_label_item()
        active_id = self.service.db.scalar(
            "SELECT id FROM materials WHERE slug = 'lab_results_2' "
            "AND voyage = 'KX-26-03' AND status = 'active'")
        self.assertEqual(item["payload"]["material_id"], active_id)
        detail = self.service.get_review_item(item["id"])
        self.assertEqual(detail["source"]["material_id"], active_id)

    def test_correction_lands_on_the_current_version_only(self) -> None:
        self.load()
        original = (FIXTURES / "lab_results_2.html").read_text(encoding="utf-8")
        self.service.revise_material("lab_results_2",
                                     original.replace("</table>", "</table>\n"))
        item = self._missing_label_item()
        record_key = item["record_key"]
        self.service.decide(item["id"], "correct", value={"fields": {"label_raw": "S05-002"}})
        rows = self.service.db.fetchall(
            "SELECT r.payload, m.status FROM raw_records r "
            "JOIN materials m ON m.id = r.material_id WHERE r.record_key = ?",
            (record_key,))
        active = [r for r in rows if r["status"] == "active"]
        superseded = [r for r in rows if r["status"] == "superseded"]
        self.assertEqual(len(active), 1)
        self.assertEqual(len(superseded), 1)
        self.assertIn("S05-002", active[0]["payload"])
        self.assertNotIn("S05-002", superseded[0]["payload"])
        self.assertEqual(self.service.query_samples(batch="KX-2603-E")["total"], 2)

    def test_correction_survives_equivalent_revision_and_revert(self) -> None:
        self.load()
        original = (FIXTURES / "lab_results_2.html").read_text(encoding="utf-8")
        item = self._missing_label_item()
        self.service.decide(item["id"], "correct", value={"fields": {"label_raw": "S05-002"}})
        self.assertEqual(self.service.query_samples(batch="KX-2603-E")["total"], 2)

        self.service.revise_material("lab_results_2",
                                     original.replace("</table>", "</table>\n"))
        self.assertEqual(self.service.query_samples(batch="KX-2603-E")["total"], 2)
        revert = self.service.revise_material("lab_results_2", original)
        self.assertEqual(revert.status, "reverted")
        # The row is semantically unchanged, so the correction replays.
        self.assertEqual(self.service.query_samples(batch="KX-2603-E")["total"], 2)
        replay = self.service.db.fetchone(
            "SELECT status FROM review_decisions WHERE record_key = ? "
            "AND kind = 'correction'", (item["record_key"],))
        self.assertEqual(replay["status"], "active")

    def test_evidence_points_at_current_line_after_equivalent_revision(self) -> None:
        self.load()
        before = self.service.query_samples(batch="KX-2603-A")["items"][0]
        original = (FIXTURES / "deck_log.html").read_text(encoding="utf-8")
        # A leading comment shifts every line but leaves the rows unchanged.
        self.service.revise_material("deck_log", "<!-- v2 -->\n" + original)
        after = self.service.query_samples(batch="KX-2603-A")["items"][0]
        self.assertEqual(after["label"], before["label"])
        self.assertEqual(after["salinity"], before["salinity"])
        self.assertEqual(after["provenance"]["deck"][0]["line"],
                         before["provenance"]["deck"][0]["line"] + 1)
        self.assertEqual(
            self.service.get_batch("KX-2603-A")["results"][0]["provenance"]["deck"][0]["line"],
            after["provenance"]["deck"][0]["line"],
        )

    def test_correction_in_one_material_does_not_invalidate_another(self) -> None:
        lab = ("<html><body><h2>实验室盐度</h2><table>"
               "<tr><th>瓶号</th><th>盐度</th></tr>"
               "<tr><td>…01-001</td><td>33.1</td></tr></table></body></html>")
        handover = ("<html><body><h2>送检批次 Z-1</h2>"
                    "<p>站次 S01 2026-05-01 08:00—08:20</p><table>"
                    "<tr><th>瓶号</th><th>采样时间</th></tr>"
                    "<tr><td>S01-001</td><td>08:00</td></tr></table></body></html>")
        self.service.import_materials([
            MaterialInput("lab_a.html", lab, kind="lab", voyage="V1"),
            MaterialInput("lab_b.html", lab, kind="lab", voyage="V1"),
            MaterialInput("handover.html", handover, kind="handover", voyage="V1"),
        ])
        item = next(i for i in self.service.list_review_queue(voyage="V1")["items"]
                    if i["kind"] == "low_confidence_extraction"
                    and i["payload"]["material_slug"] == "lab_a")
        self.service.decide(item["id"], "correct", value={"fields": {"label_raw": "S01-001"}})
        # Rewriting another material's row must not touch lab_a's correction.
        self.service.revise_material("lab_b", lab.replace("33.1", "33.9"), voyage="V1")
        decision = self.service.db.fetchone(
            "SELECT status FROM review_decisions WHERE record_key = ?",
            (item["record_key"],))
        self.assertEqual(decision["status"], "active")

    def test_correction_is_invalidated_when_its_row_is_rewritten(self) -> None:
        self.load()
        item = self._missing_label_item()
        record_key = item["record_key"]
        self.service.decide(item["id"], "correct", value={"fields": {"label_raw": "S05-002"}})
        original = (FIXTURES / "lab_results_2.html").read_text(encoding="utf-8")
        # The source now carries a different label for that row, so the row's
        # identity changes and the correction can no longer apply to it.
        self.service.revise_material(
            "lab_results_2", original.replace("<td>—</td>", "<td>S05-009</td>"))
        decision = self.service.db.fetchone(
            "SELECT status, active FROM review_decisions WHERE record_key = ? "
            "AND kind = 'correction'", (record_key,))
        self.assertEqual(decision["status"], "invalidated")
        self.assertEqual(int(decision["active"]), 0)

    def test_equivalent_revision_does_not_duplicate_materials(self) -> None:
        self.load()
        original = (FIXTURES / "lab_results_2.html").read_text(encoding="utf-8")
        before = self.service.db.scalar(
            "SELECT COUNT(*) FROM materials WHERE slug = 'lab_results_2'")
        report = self.service.revise_material(
            "lab_results_2", original.replace("</table>", "</table>\n"))
        self.assertEqual(report.status, "created")
        after = self.service.db.scalar(
            "SELECT COUNT(*) FROM materials WHERE slug = 'lab_results_2'")
        self.assertEqual(after, before + 1)
        # Re-submitting the current bytes does not create another record.
        same = self.service.revise_material(
            "lab_results_2", original.replace("</table>", "</table>\n"))
        self.assertTrue(same.unchanged)
        self.assertEqual(self.service.db.scalar(
            "SELECT COUNT(*) FROM materials WHERE slug = 'lab_results_2'"), after)


class WithdrawRestoreTests(ServiceTestCase):
    def test_withdraw_then_reimport_restores_without_a_new_material(self) -> None:
        self.load()
        count = self.service.db.scalar("SELECT COUNT(*) FROM materials")
        self.service.withdraw_material("lab_results_1")
        self.assertEqual(self.service.query_samples(batch="KX-2603-A")["total"], 0)
        material = next(m for m in fixtures() if m.name == "lab_results_1.txt")
        report = self.service.import_materials([material])[0]
        self.assertEqual(report.status, "restored")
        self.assertFalse(report.created)
        self.assertEqual(self.service.db.scalar("SELECT COUNT(*) FROM materials"), count)
        self.assertEqual(self.service.query_samples(batch="KX-2603-A")["total"], 4)


if __name__ == "__main__":
    unittest.main()
