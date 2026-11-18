"""Batch pairing rules: whole-batch consistency, exclusivity, review flow."""

from __future__ import annotations

import unittest

from helpers import FIXTURES, ServiceTestCase, fixtures, material


class PairingRuleTests(ServiceTestCase):
    def test_only_consistent_pairings_are_published(self) -> None:
        self.load()
        batches = self.batches()
        for code in ("KX-2603-A", "KX-2603-B", "KX-2603-C", "KX-2603-D",
                     "KX-2603-F", "KX-2603-H"):
            self.assertEqual(batches[code]["status"], "published", code)
            self.assertEqual(batches[code]["published_count"], batches[code]["bottle_count"])
            results = self.batch(code)["results"]
            self.assertTrue(all(r["result_key"] for r in results))

    def test_batch_with_missing_result_is_held_not_partly_published(self) -> None:
        self.load()
        batch = self.batch("KX-2603-E")
        self.assertEqual(batch["status"], "held")
        self.assertEqual(batch["published_count"], 0)
        self.assertEqual(batch["pending_count"], 2)
        samples = self.service.query_samples(batch="KX-2603-E")
        self.assertEqual(samples["total"], 0)
        hold = [i for i in self.open_items("KX-2603-E") if i["kind"] == "batch_hold"]
        self.assertEqual(len(hold), 1)
        self.assertIn("no complete pairing", hold[0]["detail"])

    def test_ambiguous_batch_publishes_only_invariant_pairs(self) -> None:
        self.load()
        batch = self.batch("KX-2603-G")
        self.assertEqual(batch["status"], "in_review")
        self.assertEqual(batch["bottle_count"], 4)
        self.assertEqual(batch["published_count"], 2)
        states = {r["label"]: r["state"] for r in batch["results"]}
        self.assertEqual(states["S07-001"], "auto")
        self.assertEqual(states["S07-002"], "auto")
        self.assertEqual(states["S07-003"], "pending")
        ambiguous = {i["record_key"]: i for i in self.open_items("KX-2603-G", "ambiguous_assignment")}
        self.assertEqual(len(ambiguous), 2)

    def test_all_held_batches_continue_processing(self) -> None:
        self.load()
        statuses = {b["batch_code"]: b["status"] for b in self.service.list_batches()}
        # One held batch must not stop the others.
        self.assertEqual(sum(1 for s in statuses.values() if s in ("published", "in_review")), 7)

    def test_one_result_never_belongs_to_two_bottles(self) -> None:
        self.load()
        seen: dict[str, str] = {}
        for batch in self.service.list_batches():
            for row in self.batch(batch["batch_code"])["results"]:
                if not row["result_key"]:
                    continue
                self.assertNotIn(row["result_key"], seen,
                                 f"{row['result_key']} used by {seen.get(row['result_key'])} "
                                 f"and {batch['batch_code']}")
                seen[row["result_key"]] = batch["batch_code"]

    def test_bottle_only_in_one_batch(self) -> None:
        self.load()
        # A batch A bottle is never published against batch H's result.
        a_results = {r["result_key"] for r in self.batch("KX-2603-A")["results"]}
        h_results = {r["result_key"] for r in self.batch("KX-2603-H")["results"]}
        self.assertFalse(a_results & h_results)


class ReviewFlowTests(ServiceTestCase):
    def test_confirming_ambiguity_publishes_batch(self) -> None:
        self.load()
        items = self.open_items("KX-2603-G", "ambiguous_assignment")
        self.assertTrue(items)
        item = items[0]
        self.assertEqual(len(item["candidates"]), 2)
        chosen = item["candidates"][0]["result"]
        self.service.decide(item["id"], "confirm", reviewer="张敏",
                            note="按抄录顺序核对", value={"result": chosen})
        batch = self.batch("KX-2603-G")
        self.assertEqual(batch["status"], "published")
        self.assertEqual(batch["published_count"], 4)
        self.assertTrue(all(r["state"] == "confirmed" for r in batch["results"]))
        published = self.service.query_samples(batch="KX-2603-G")
        self.assertEqual(published["total"], 4)
        by_label = {r["label"]: r["salinity"] for r in published["items"]}
        self.assertEqual(by_label["S07-004"], 34.06)

    def test_hold_cannot_be_filled_with_unsupported_result(self) -> None:
        self.load()
        hold = [i for i in self.open_items("KX-2603-E") if i["kind"] == "batch_hold"][0]
        bottles = hold["payload"]["bottles"]
        pairs = [{"bottle": b["bottle"], "result": "lab_missing"} for b in bottles]
        with self.assertRaises(Exception):
            self.service.decide(hold["id"], "confirm", value={"pairs": pairs})
        self.assertEqual(self.batch("KX-2603-E")["status"], "held")

    def test_correcting_missing_label_unblocks_held_batch(self) -> None:
        self.load()
        self.assertEqual(self.batch("KX-2603-E")["status"], "held")
        warning = [i for i in self.open_items("KX-2603-E", "low_confidence_extraction")
                   if i["payload"]["fields"].get("label_raw") == "—"]
        self.assertTrue(warning)
        item = warning[0]
        self.service.decide(item["id"], "correct", reviewer="张敏",
                            value={"fields": {"label_raw": "S05-002"}})
        batch = self.batch("KX-2603-E")
        self.assertEqual(batch["status"], "published")
        self.assertEqual(batch["published_count"], 2)
        self.assertEqual(self.service.query_samples(batch="KX-2603-E")["total"], 2)

    def test_invalid_pairing_is_rejected(self) -> None:
        self.load()
        item = self.open_items("KX-2603-G", "ambiguous_assignment")[0]
        with self.assertRaises(Exception):
            self.service.decide(item["id"], "confirm", value={"result": "lab_does_not_exist"})

    def test_review_item_exposes_source_text(self) -> None:
        self.load()
        item = self.open_items("KX-2603-G", "ambiguous_assignment")[0]
        detail = self.service.get_review_item(item["id"])
        self.assertIn("source", detail)
        self.assertIn("KX-2603-G", detail["source"]["content"])
        self.assertIn("line", detail["locator"])

    def test_correcting_low_confidence_label_repairs_match(self) -> None:
        self.load()
        # Batch E is held because one bottle has no laboratory result.
        items = self.open_items("KX-2603-E", "low_confidence_extraction")
        self.assertTrue(items)
        item = items[0]
        record_key = item["record_key"]
        self.service.decide(item["id"], "correct", reviewer="张敏",
                            value={"fields": {"label_raw": "S05-002"}})
        corrected = self.service.db.fetchone(
            "SELECT payload FROM raw_records WHERE record_key = ?", (record_key,)
        )
        self.assertIn("S05-002", corrected["payload"])
        # The correction alone cannot invent a result, so E stays held.
        self.assertEqual(self.batch("KX-2603-E")["status"], "held")

    def test_invalidation_is_explained_in_queue(self) -> None:
        self.load()
        item = self.open_items("KX-2603-G", "ambiguous_assignment")[0]
        self.service.decide(item["id"], "confirm",
                            value={"result": item["candidates"][0]["result"]})
        handover = (FIXTURES / "handover.html").read_text(encoding="utf-8")
        self.service.revise_material("handover", handover.replace("S07-004", "S07-006"))
        reopened = self.open_items("KX-2603-G", "ambiguous_assignment")
        self.assertTrue(reopened)
        self.assertTrue(any("set aside" in i["detail"] for i in reopened))

    def test_reject_marks_item_stale_without_publishing(self) -> None:
        self.load()
        item = self.open_items("KX-2603-G", "ambiguous_assignment")[0]
        self.service.decide(item["id"], "reject", note="无法判定")
        self.assertEqual(self.batch("KX-2603-G")["status"], "in_review")
        self.assertEqual(len(self.open_items("KX-2603-G", "ambiguous_assignment")), 1)


if __name__ == "__main__":
    unittest.main()
