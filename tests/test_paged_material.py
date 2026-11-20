"""Page-wise collection of a long deck table.

The fixtures under ``fixtures/paged_deck`` are one four-page deck log plus the
handover sheet and laboratory report for the same voyage.  These tests upload
the deck pages out of order, re-send pages, interrupt the process and finish
the set, and check that queries keep serving the last complete version until
the switch.
"""

from __future__ import annotations

import unittest

from helpers import FIXTURES, ServiceTestCase

from app.config import Settings
from app.document import PageInput
from app.service import MaterialInput, SamplingService

PAGED = FIXTURES / "paged_deck"
VOYAGE = "PG-26-01"


def page(number: int, content: str | None = None, name: str | None = None) -> PageInput:
    text = content if content is not None else (PAGED / f"deck_p{number}.txt").read_text("utf-8")
    return PageInput(page_no=number, name=name or f"deck_p{number}.txt", content=text)


class PagedDeckCase(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.service.import_materials([
            MaterialInput(name="handover.html",
                          content=(PAGED / "handover.html").read_text("utf-8"),
                          kind="handover", voyage=VOYAGE),
            MaterialInput(name="lab_results.txt",
                          content=(PAGED / "lab_results.txt").read_text("utf-8"),
                          kind="lab", voyage=VOYAGE),
        ])

    def upload(self, *pages: PageInput, version: int | None = None,
               expected: int = 4):
        return self.service.import_pages([MaterialInput(
            name="deck", content="", kind="deck", voyage=VOYAGE, slug="deck",
            pages=list(pages), version=version, expected_pages=expected,
        )])[0]

    def statuses(self) -> dict[str, str]:
        return {b["batch_code"]: b["status"] for b in self.service.list_batches(voyage=VOYAGE)}

    def deck_rows(self) -> list[dict]:
        material_row = self.service.get_material("deck", voyage=VOYAGE)
        return material_row["raw_records"]


class OutOfOrderCollectionTests(PagedDeckCase):
    def test_pages_in_any_order_activate_only_when_all_arrive(self) -> None:
        self.assertEqual(self.upload(page(3), page(1)).status, "collecting")
        # A deck table is supporting evidence; while its pages are missing the
        # material does not exist at all, so no deck record is exposed.
        with self.assertRaises(Exception):
            self.service.get_material("deck", voyage=VOYAGE)
        self.assertEqual(self.upload(page(2)).missing_pages, [4])
        final = self.upload(page(4))
        self.assertEqual(final.status, "activated")
        self.assertEqual(self.statuses(), {"PG-2601-A": "published", "PG-2601-B": "published"})
        self.assertEqual(len(self.deck_rows()), 10)
        self.assertEqual(self.service.query_samples(voyage=VOYAGE)["total"], 10)

    def test_repeated_page_is_a_noop(self) -> None:
        self.upload(page(1))
        again = self.upload(page(1))
        self.assertEqual(again.status, "collecting")
        self.assertEqual(again.unchanged_pages, [1])
        self.assertEqual(again.stored_pages, [])
        self.assertEqual(again.received_pages, [1])

    def test_identical_resend_of_complete_version_changes_nothing(self) -> None:
        for number in (1, 2, 3, 4):
            self.upload(page(number))
        before = {b["batch_code"]: b["version"] for b in
                  self.service.list_batches(voyage=VOYAGE)}
        again = self.upload(page(2), version=1)
        self.assertEqual(again.status, "unchanged")
        self.assertFalse(again.activated)
        after = {b["batch_code"]: b["version"] for b in
                 self.service.list_batches(voyage=VOYAGE)}
        self.assertEqual(before, after)

    def test_continuation_page_inherits_station_date_and_columns(self) -> None:
        for number in (1, 2, 3, 4):
            self.upload(page(number))
        rows = {row["fields"]["label_norm"]: row for row in self.deck_rows()}
        # Page 2 and page 4 restate neither station heading nor column header.
        for label in ("S11-004", "S11-005"):
            self.assertEqual(rows[label]["fields"]["station"], "S11")
            self.assertEqual(rows[label]["fields"]["sampled_at"][:10], "2026-04-01")
        for label in ("S12-004", "S12-005"):
            self.assertEqual(rows[label]["fields"]["station"], "S12")
            self.assertEqual(rows[label]["fields"]["sampled_at"][:10], "2026-04-02")
        self.assertEqual(len(rows), 10)

    def test_evidence_points_at_the_page_file_and_line(self) -> None:
        for number in (1, 2, 3, 4):
            self.upload(page(number))
        rows = {row["fields"]["label_norm"]: row for row in self.deck_rows()}
        second = rows["S11-004"]["locator"]
        self.assertEqual(second["file"], "deck_p2.txt")
        self.assertEqual(second["page"], 2)
        self.assertEqual(second["line"], 4)
        self.assertIn("S11-004", second["excerpt"])
        samples = self.service.query_samples(voyage=VOYAGE, batch="PG-2601-A")["items"]
        provenance = next(item for item in samples if item["label"] == "S11-004")
        self.assertEqual(provenance["provenance"]["deck"][0]["file"], "deck_p2.txt")
        self.assertEqual(provenance["provenance"]["deck"][0]["line"], 4)

    def test_restart_resumes_collection(self) -> None:
        self.upload(page(1), page(3))
        reopened = SamplingService(settings=Settings(
            database_url=self.service.settings.database_url,
            fixture_dir=self.service.settings.fixture_dir,
        ))
        self.addCleanup(reopened.db.close)
        state = reopened.get_page_set("deck", voyage=VOYAGE)
        self.assertEqual(state["missing_pages"], [2, 4])
        report = reopened.import_pages([MaterialInput(
            name="deck", content="", kind="deck", voyage=VOYAGE, slug="deck",
            pages=[page(2), page(4)], expected_pages=4,
        )])[0]
        self.assertEqual(report.status, "activated")
        self.assertEqual(report.version, 1)


class VersionSwitchTests(PagedDeckCase):
    def complete_v1(self) -> None:
        for number in (1, 2, 3, 4):
            self.upload(page(number))

    def test_incomplete_next_version_keeps_serving_the_old_one(self) -> None:
        self.complete_v1()
        before = self.service.query_samples(voyage=VOYAGE)["items"]
        revised = (PAGED / "deck_p1.txt").read_text("utf-8").replace(
            "S11-003\t08:40\t50", "S11-003\t08:45\t50")
        report = self.upload(page(1, revised))
        self.assertEqual(report.status, "collecting")
        self.assertEqual(report.version, 2)
        self.assertEqual(report.affected_batches, [])
        after = self.service.query_samples(voyage=VOYAGE)["items"]
        self.assertEqual(before, after)
        self.assertEqual(self.service.get_material("deck", voyage=VOYAGE)["version"], 1)

    def test_completed_next_version_switches_at_once(self) -> None:
        self.complete_v1()
        revised = (PAGED / "deck_p1.txt").read_text("utf-8").replace(
            "S11-003\t08:40\t50", "S11-003\t08:45\t50")
        self.upload(page(1, revised), version=2)
        report = self.upload(page(2), page(3), page(4), version=2)
        self.assertEqual(report.status, "activated")
        self.assertEqual(report.affected_batches,
                         [f"{VOYAGE}|PG-2601-A", f"{VOYAGE}|PG-2601-B"])
        self.assertEqual(self.service.get_material("deck", voyage=VOYAGE)["version"], 2)
        rows = {row["fields"]["label_norm"]: row for row in self.deck_rows()}
        self.assertEqual(rows["S11-003"]["fields"]["sampled_at"], "2026-04-01T08:45")
        self.assertEqual(self.service.query_samples(voyage=VOYAGE)["total"], 10)

    def test_late_old_version_pages_do_not_replace_the_active_version(self) -> None:
        self.complete_v1()
        revised = (PAGED / "deck_p1.txt").read_text("utf-8").replace(
            "S11-003\t08:40\t50", "S11-003\t08:45\t50")
        self.upload(page(1, revised), version=2)
        self.upload(page(2), page(3), page(4), version=2)
        active_before = self.service.query_samples(voyage=VOYAGE)["items"]
        late = self.upload(page(2, (PAGED / "deck_p2.txt").read_text("utf-8")
                               .replace("08:55", "08:56")), version=1)
        self.assertEqual(late.status, "late_page")
        self.assertFalse(late.activated)
        self.assertEqual(late.affected_batches, [])
        self.assertEqual(self.service.query_samples(voyage=VOYAGE)["items"], active_before)
        self.assertEqual(self.service.get_material("deck", voyage=VOYAGE)["version"], 2)
        old_set = self.service.get_page_set("deck", voyage=VOYAGE, version=1)
        self.assertEqual(old_set["status"], "complete")
        self.assertFalse(old_set["active"])
        self.assertIn(2, old_set["received_pages"])

    def test_late_page_without_a_version_is_routed_to_the_older_version(self) -> None:
        self.complete_v1()
        revised = (PAGED / "deck_p1.txt").read_text("utf-8").replace(
            "S11-003\t08:40\t50", "S11-003\t08:45\t50")
        self.upload(page(1, revised), version=2)
        self.upload(page(2), version=2)
        # An identical re-send is recognised by content even while version 2
        # is still collecting, and goes back to the version that holds it.
        late = self.upload(page(3))
        self.assertEqual(late.version, 1)
        self.assertIn(late.status, ("late_page", "unchanged"))
        self.assertFalse(late.activated)
        self.assertEqual(self.service.get_page_set(
            "deck", voyage=VOYAGE, version=1)["missing_pages"], [])
        self.assertTrue(self.service.get_page_set(
            "deck", voyage=VOYAGE, version=1)["active"])
        self.assertEqual(self.service.get_page_set(
            "deck", voyage=VOYAGE, version=2)["missing_pages"], [3, 4])
        # A changed page sent with ``version`` while the old version is still
        # the active one corrects that active version; it is never mistaken for
        # part of the newer version still being collected.
        late = self.upload(page(4, (PAGED / "deck_p4.txt").read_text("utf-8")
                               .replace("11:40", "11:45")), version=1)
        self.assertEqual(late.version, 1)
        self.assertIn(late.status, ("resubmitted", "late_page"))
        self.assertEqual(self.service.get_page_set(
            "deck", voyage=VOYAGE, version=2)["missing_pages"], [3, 4])
        self.assertEqual(self.service.get_material("deck", voyage=VOYAGE)["version"], 1)

    def test_late_page_of_an_older_version_is_archived_without_affecting_active(self) -> None:
        # Version 2 completes before version 1 was ever received in full.
        self.upload(page(1), page(2), page(3), page(4), version=2)
        late_reports = [self.upload(page(n), version=1) for n in (1, 2, 3, 4)]
        self.assertTrue(all(r.status == "late_page" for r in late_reports))
        self.assertTrue(all(not r.activated for r in late_reports))
        self.assertEqual(self.service.get_material("deck", voyage=VOYAGE)["version"], 2)
        versions = {p["version"]: p for p in self.service.list_page_sets(voyage=VOYAGE)}
        self.assertEqual(versions[1]["status"], "complete")
        self.assertFalse(versions[1]["active"])
        self.assertIsNotNone(versions[1]["material_id"])

    def test_equal_meaning_repagination_keeps_review_decisions(self) -> None:
        # Two labels are transcribed as fragments, so batch A needs a
        # reviewer's decision.
        lab = (PAGED / "lab_results.txt").read_text("utf-8")
        lab = (lab.replace("LAB-PA-02\tS11-002", "LAB-PA-02\tS11-00…")
                  .replace("LAB-PA-03\tS11-003", "LAB-PA-03\tS11-00…"))
        self.service.revise_material("lab_results", lab, voyage=VOYAGE)
        for number in (1, 2, 3, 4):
            self.upload(page(number))
        item = next(i for i in self.service.list_review_queue(status="open", voyage=VOYAGE)["items"]
                    if i["kind"] == "ambiguous_assignment")
        chosen = item["candidates"][0]["result"]
        self.service.decide(item["id"], "confirm", value={"result": chosen})
        self.assertEqual(self.statuses()["PG-2601-A"], "published")

        # The same table is re-uploaded split into two larger pages.  Rows keep
        # their meaning, so the reviewer's pairing stays effective.
        whole = "\n".join((PAGED / f"deck_p{n}.txt").read_text("utf-8") for n in (1, 2, 3, 4))
        first, rest = whole.split("第 3 页 / 共 4 页", 1)
        report = self.service.import_pages([MaterialInput(
            name="deck", content="", kind="deck", voyage=VOYAGE, slug="deck",
            pages=[PageInput(1, "a.txt", first),
                   PageInput(2, "b.txt", "第 3 页 / 共 4 页" + rest)],
            version=2, expected_pages=2,
        )])[0]
        self.assertEqual(report.status, "activated")
        self.assertEqual(self.statuses(), {"PG-2601-A": "published", "PG-2601-B": "published"})
        decisions = self.service.db.fetchall(
            "SELECT status, active FROM review_decisions "
            "WHERE batch_code = 'PG-2601-A' AND kind = 'assignment'")
        self.assertTrue(decisions)
        self.assertTrue(all(d["active"] == 1 for d in decisions))
        self.assertEqual({d["status"] for d in decisions}, {"active"})
        published = self.service.query_samples(voyage=VOYAGE, batch="PG-2601-A")["items"]
        self.assertEqual(len(published), 5)
        self.assertEqual({item["state"] for item in published}, {"confirmed"})
        # Evidence follows the new page files, not the old ones.
        for item in published:
            self.assertIn(item["provenance"]["deck"][0]["file"], ("a.txt", "b.txt"))


if __name__ == "__main__":
    unittest.main()
