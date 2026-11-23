"""Re-uploads of batch-segmented laboratory reports (补传).

A revised report may be missing a batch's section, keep only its heading, or
carry a salinity table nothing can be read from.  The batch then keeps the
most recent usable section from an earlier version of the same report, the
import result explains why, and queries/reviews keep pointing at the
manuscript version each batch actually reads.  Withdrawing the report stops
the carried sections as well.
"""

from __future__ import annotations

import unittest

from helpers import FIXTURES, ServiceTestCase, VOYAGE

from app.service import MaterialInput, SamplingService

LAB1 = "lab_results_1"
LAB2 = "lab_results_2"


def upload(service: SamplingService, content: str, name: str = "lab_results_1.txt"):
    """A fresh upload of a laboratory report (what a 补传 submits)."""
    return service.import_materials([
        MaterialInput(name=name, content=content, kind="lab", voyage=VOYAGE)
    ])[0]


def lab1_text() -> str:
    return (FIXTURES / "lab_results_1.txt").read_text(encoding="utf-8")


def lab2_html() -> str:
    return (FIXTURES / "lab_results_2.html").read_text(encoding="utf-8")


def drop_text_section(text: str, code: str, keep_heading: bool = False) -> str:
    """Remove one ``送检批次 <code>`` section from the text report."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if code in line)
    following = [i for i, line in enumerate(lines)
                 if line.startswith("送检批次") and i > start]
    end = following[0] if following else len(lines)
    kept = lines[:start] + ([lines[start]] if keep_heading else []) + lines[end:]
    return "\n".join(kept) + "\n"


def drop_html_table(text: str, code: str) -> str:
    """Keep the batch heading but remove the table that follows it."""
    heading = text.index(f"<h2>送检批次 {code}")
    start = text.index("<table>", heading)
    end = text.index("</table>", start) + len("</table>")
    return text[:start] + text[end:]


class CarryOverOnRevisionTests(ServiceTestCase):
    def test_missing_section_keeps_most_recent_usable_version(self) -> None:
        self.load()
        revised = drop_text_section(lab1_text(), "KX-2603-B")
        revised = revised.replace("33.842", "33.900")  # batch A updates as usual
        report = self.service.revise_material(LAB1, revised)

        self.assertEqual(len(report.carried_over), 1)
        carried = report.carried_over[0]
        self.assertEqual(carried["batch"], "KX-2603-B")
        self.assertEqual(carried["reason"], "missing")
        self.assertEqual(carried["records"], 3)
        self.assertEqual(carried["source_version"], 1)
        # The import result explains, in words, why the section was kept.
        self.assertTrue(any("KX-2603-B" in w and "沿用" in w for w in report.warnings))

        batch_b = self.batch("KX-2603-B")
        self.assertEqual(batch_b["status"], "published")
        self.assertEqual(
            {r["label"]: r["salinity"] for r in batch_b["results"]},
            {"S02-001": 33.901, "S02-002": 34.024, "S02-003": 34.077},
        )
        # Batch B still reads version 1; the updated batch A reads version 2.
        self.assertEqual(
            {r["provenance"]["lab"]["material_version"] for r in batch_b["results"]}, {1})
        batch_a = self.batch("KX-2603-A")
        self.assertEqual(
            {r["label"]: r["salinity"] for r in batch_a["results"]}["S01-001"], 33.900)
        self.assertEqual(
            {r["provenance"]["lab"]["material_version"] for r in batch_a["results"]}, {2})

    def test_heading_only_section_is_carried_over(self) -> None:
        self.load()
        revised = drop_text_section(lab1_text(), "KX-2603-C", keep_heading=True)
        report = self.service.revise_material(LAB1, revised)
        self.assertEqual([c["batch"] for c in report.carried_over], ["KX-2603-C"])
        self.assertEqual(report.carried_over[0]["reason"], "heading_only")
        batch = self.batch("KX-2603-C")
        self.assertEqual(batch["status"], "published")
        self.assertEqual(len(batch["results"]), 2)
        self.assertEqual(
            {r["provenance"]["lab"]["material_version"] for r in batch["results"]}, {1})

    def test_unreadable_salinity_table_is_carried_over(self) -> None:
        self.load()
        revised = lab1_text()
        for value in ("33.901", "34.024", "34.077"):  # all of batch B's values
            revised = revised.replace(value, "///")
        report = self.service.revise_material(LAB1, revised)
        self.assertEqual([c["batch"] for c in report.carried_over], ["KX-2603-B"])
        self.assertEqual(report.carried_over[0]["reason"], "unreadable")
        batch = self.batch("KX-2603-B")
        self.assertEqual(batch["status"], "published")
        self.assertEqual(
            {r["label"]: r["salinity"] for r in batch["results"]}["S02-002"], 34.024)

    def test_unaffected_batches_are_not_carried(self) -> None:
        self.load()
        report = self.service.revise_material(LAB1, drop_text_section(lab1_text(), "KX-2603-B"))
        self.assertEqual([c["batch"] for c in report.carried_over], ["KX-2603-B"])
        # The other laboratory report is untouched by the revision.
        self.assertEqual(self.batch("KX-2603-F")["status"], "published")
        self.assertEqual(
            {r["provenance"]["lab"]["material"] for r in self.batch("KX-2603-F")["results"]},
            {"lab_results_2"},
        )


class AlternatingAndRepeatedUploadTests(ServiceTestCase):
    def test_alternating_missing_sections_across_consecutive_versions(self) -> None:
        """v2 drops batch B, v3 restores B but drops batch C."""
        self.load()
        self.service.revise_material(LAB1, drop_text_section(lab1_text(), "KX-2603-B"))
        self.assertEqual(
            {r["provenance"]["lab"]["material_version"] for r in self.batch("KX-2603-B")["results"]},
            {1},
        )
        report = self.service.revise_material(LAB1, drop_text_section(lab1_text(), "KX-2603-C"))
        # C's most recent usable section is version 2 (v1 was already replaced).
        self.assertEqual([(c["batch"], c["source_version"]) for c in report.carried_over],
                         [("KX-2603-C", 2)])
        expected_versions = {"KX-2603-A": 3, "KX-2603-B": 3, "KX-2603-C": 2, "KX-2603-D": 3}
        for code, version in expected_versions.items():
            batch = self.batch(code)
            self.assertEqual(batch["status"], "published", code)
            self.assertEqual(
                {r["provenance"]["lab"]["material_version"] for r in batch["results"]},
                {version}, code,
            )

    def test_repeated_defective_reupload_is_a_noop(self) -> None:
        self.load()
        defective = drop_text_section(lab1_text(), "KX-2603-B")
        first = self.service.revise_material(LAB1, defective)
        self.assertEqual([c["batch"] for c in first.carried_over], ["KX-2603-B"])
        version_before = self.batch("KX-2603-B")["version"]
        again = self.service.revise_material(LAB1, defective)
        self.assertEqual(again.status, "unchanged")
        self.assertEqual(again.carried_over, [])
        self.assertEqual(self.batch("KX-2603-B")["version"], version_before)
        self.assertEqual(self.batch("KX-2603-B")["status"], "published")

    def test_second_defective_version_still_uses_the_original_section(self) -> None:
        """Two different re-uploads both missing B: the source stays version 1."""
        self.load()
        first = drop_text_section(lab1_text(), "KX-2603-B").replace("33.842", "33.900")
        self.service.revise_material(LAB1, first)
        second = drop_text_section(lab1_text(), "KX-2603-B").replace("33.842", "33.901")
        report = self.service.revise_material(LAB1, second)
        # v2 never had a usable B section, so v3 keeps using version 1.
        self.assertEqual([(c["batch"], c["source_version"]) for c in report.carried_over],
                         [("KX-2603-B", 1)])
        batch = self.batch("KX-2603-B")
        self.assertEqual(batch["status"], "published")
        self.assertEqual(
            {r["provenance"]["lab"]["material_version"] for r in batch["results"]}, {1})


class WithdrawalTests(ServiceTestCase):
    def test_withdrawal_stops_carried_sections(self) -> None:
        self.load()
        self.service.revise_material(LAB1, drop_text_section(lab1_text(), "KX-2603-B"))
        self.assertEqual(self.batch("KX-2603-B")["status"], "published")
        report = self.service.withdraw_material(LAB1)
        for code in ("KX-2603-A", "KX-2603-B", "KX-2603-C", "KX-2603-D"):
            self.assertEqual(self.batch(code)["status"], "held", code)
            self.assertEqual(self.service.query_samples(batch=code)["total"], 0, code)
            self.assertIn(f"KX-26-03|{code}", report.affected_batches)
        # The carried rows themselves are deactivated, not just unlinked.
        inactive = self.service.db.scalar(
            "SELECT COUNT(*) FROM lab_results lr "
            "JOIN batch_results br ON br.lab_result_id = lr.record_key "
            "JOIN batches b ON b.id = br.batch_id "
            "WHERE b.batch_code = 'KX-2603-B' AND lr.active = 1",
            (), 0)
        self.assertEqual(int(inactive or 0), 0)
        # A batch fed by the other laboratory report keeps working.
        self.assertEqual(self.batch("KX-2603-F")["status"], "published")

    def test_fresh_upload_after_withdrawal_does_not_resurrect_sections(self) -> None:
        self.load()
        self.service.revise_material(LAB1, drop_text_section(lab1_text(), "KX-2603-B"))
        self.service.withdraw_material(LAB1)
        # A genuinely new report uploaded afterwards starts clean: the
        # withdrawn report's sections must not leak back in.
        fresh = drop_text_section(lab1_text(), "KX-2603-B").replace("34.108", "34.200")
        report = upload(self.service, fresh)
        self.assertEqual(report.carried_over, [])
        self.assertEqual(self.batch("KX-2603-B")["status"], "held")
        self.assertEqual(self.batch("KX-2603-A")["status"], "published")

    def test_restoring_the_same_report_restores_its_carried_sections(self) -> None:
        self.load()
        defective = drop_text_section(lab1_text(), "KX-2603-B")
        self.service.revise_material(LAB1, defective)
        self.service.withdraw_material(LAB1)
        self.assertEqual(self.batch("KX-2603-B")["status"], "held")
        restored = upload(self.service, defective)
        self.assertEqual(restored.status, "restored")
        batch = self.batch("KX-2603-B")
        self.assertEqual(batch["status"], "published")
        self.assertEqual(
            {r["provenance"]["lab"]["material_version"] for r in batch["results"]}, {1})


class DecisionAndProvenanceTests(ServiceTestCase):
    def _confirm_g(self) -> None:
        item = self.open_items("KX-2603-G", "ambiguous_assignment")[0]
        self.service.decide(item["id"], "confirm", reviewer="张敏",
                            value={"result": item["candidates"][0]["result"]})
        self.assertEqual(self.batch("KX-2603-G")["status"], "published")

    def test_review_decision_survives_carryover_of_other_sections(self) -> None:
        self.load()
        self._confirm_g()
        revised = drop_html_table(lab2_html(), "KX-2603-F")  # F keeps only a heading
        report = self.service.revise_material(LAB2, revised)
        self.assertEqual([(c["batch"], c["reason"]) for c in report.carried_over],
                         [("KX-2603-F", "heading_only")])
        # Evidence and feasible pairings for G are unchanged: the confirmed
        # pairing and the published rows stay exactly as they were.
        batch_g = self.batch("KX-2603-G")
        self.assertEqual(batch_g["status"], "published")
        self.assertEqual({r["state"] for r in batch_g["results"]}, {"confirmed"})
        decisions = self.service.db.fetchall(
            "SELECT status FROM review_decisions WHERE batch_code = 'KX-2603-G'")
        self.assertTrue(decisions)
        self.assertTrue(all(d["status"] == "active" for d in decisions))
        # F kept its previous section and stays published.
        batch_f = self.batch("KX-2603-F")
        self.assertEqual(batch_f["status"], "published")
        self.assertEqual(
            {r["provenance"]["lab"]["material_version"] for r in batch_f["results"]}, {1})

    def test_decision_survives_when_own_section_is_carried_over(self) -> None:
        self.load()
        self._confirm_g()
        # The new report drops G's own section; G keeps its original rows and
        # therefore its confirmed pairing.
        revised = drop_html_table(lab2_html(), "KX-2603-G")
        revised = revised.replace("<h2>送检批次 KX-2603-G", "<h2>送检批次 KX-2603-G")  # keep heading
        report = self.service.revise_material(LAB2, revised)
        self.assertEqual([c["batch"] for c in report.carried_over], ["KX-2603-G"])
        batch_g = self.batch("KX-2603-G")
        self.assertEqual(batch_g["status"], "published")
        self.assertEqual({r["state"] for r in batch_g["results"]}, {"confirmed"})
        self.assertEqual(
            {r["provenance"]["lab"]["material_version"] for r in batch_g["results"]}, {1})

    def test_query_points_to_the_manuscript_version_actually_used(self) -> None:
        self.load()
        self.service.revise_material(LAB1, drop_text_section(lab1_text(), "KX-2603-B"))
        carried = self.service.query_samples(batch="KX-2603-B")["items"]
        self.assertEqual(len(carried), 3)
        for item in carried:
            lab = item["provenance"]["lab"]
            self.assertEqual(lab["material"], LAB1)
            self.assertEqual(lab["material_version"], 1)
            self.assertIn("line", lab)  # position within that version
        updated = self.service.query_samples(batch="KX-2603-A")["items"]
        self.assertEqual({i["provenance"]["lab"]["material_version"] for i in updated}, {2})

    def test_review_item_source_is_the_carried_manuscript_version(self) -> None:
        self.load()
        # E-2's bottle label is unreadable, so batch E has an open extraction
        # item; the new report makes E's table unreadable and E is carried.
        revised = lab2_html().replace("<td>34.440</td>", "<td>///</td>")
        revised = revised.replace("<td>34.512</td>", "<td>无法辨认</td>")
        report = self.service.revise_material(LAB2, revised)
        self.assertIn(("KX-2603-E", "unreadable"),
                      [(c["batch"], c["reason"]) for c in report.carried_over])
        items = [i for i in self.open_items("KX-2603-E", "low_confidence_extraction")]
        self.assertTrue(items)
        detail = self.service.get_review_item(items[0]["id"])
        # The reviewer must see the version whose rows the batch actually
        # reads, not the defective re-upload.
        self.assertEqual(detail["source"]["version"], 1)
        self.assertIn("E-2", detail["source"]["content"])


if __name__ == "__main__":
    unittest.main()
