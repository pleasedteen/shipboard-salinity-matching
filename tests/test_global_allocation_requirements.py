"""Focused coverage for voyage-wide optimum allocation and review sync."""

from __future__ import annotations

import time
import unittest

from helpers import ServiceTestCase
from app.service import MaterialInput


def handover_material(voyage: str, groups: list[tuple[str, str]]) -> MaterialInput:
    parts = ["<html><body>"]
    for code, station in groups:
        parts.append(
            f"<h2>送检批次 {code}</h2>"
            f"<p>站次 {station} 2026-05-01 08:00—08:20</p>"
            "<table><tr><th>瓶号</th><th>采样时间</th></tr>"
            f"<tr><td>{station}-001</td><td>08:00</td></tr></table>"
        )
    parts.append("</body></html>")
    return MaterialInput("handover.html", "".join(parts), kind="handover", voyage=voyage)


def lab_material(voyage: str, station: str, values: list[str]) -> MaterialInput:
    rows = "".join(
        f"<tr><td>{station}-001</td><td>{station}</td><td>{value}</td></tr>"
        for value in values
    )
    content = (
        "<html><body><table>"
        "<tr><th>瓶号</th><th>站次</th><th>盐度</th></tr>"
        f"{rows}</table></body></html>"
    )
    return MaterialInput("lab.html", content, kind="lab", voyage=voyage)


class GlobalAllocationRequirementTests(ServiceTestCase):
    def test_single_bottle_with_multiple_results_accepts_either_choice(self) -> None:
        for choice_index in range(2):
            voyage = f"SINGLE{choice_index}"
            self.service.import_materials([
                handover_material(voyage, [(f"SB-{choice_index}", "S01")]),
                lab_material(voyage, "S01", ["34.100", "34.200"]),
            ])
            batch = next(b for b in self.service.list_batches(voyage=voyage)
                         if b["batch_code"] == f"SB-{choice_index}")
            self.assertEqual(batch["status"], "in_review")
            self.assertEqual(batch["published_count"], 0)

            items = [
                i for i in self.service.list_review_queue(voyage=voyage)["items"]
                if i["kind"] == "ambiguous_assignment"
            ]
            self.assertEqual(len(items), 1)
            self.assertEqual(len(items[0]["candidates"]), 2)
            chosen = items[0]["candidates"][choice_index]

            decided = self.service.decide(
                items[0]["id"], "confirm", value={"result": chosen["result"]}
            )
            self.assertEqual(decided["status"], "resolved")

            fresh = self.service.get_batch(batch["id"])
            self.assertEqual(fresh["status"], "published")
            self.assertEqual(fresh["published_count"], 1)
            self.assertEqual(fresh["results"][0]["result_key"], chosen["result"])
            self.assertEqual(fresh["results"][0]["salinity"], chosen["salinity"])
            self.assertEqual(
                self.service.query_samples(batch=batch["id"])["total"], 1
            )
            self.assertFalse([
                i for i in self.service.list_review_queue(
                    voyage=voyage, status="open")["items"]
                if i["kind"] == "ambiguous_assignment"
            ])

    def test_reassignable_contention_chain_completes_maximum_batches(self) -> None:
        voyage = "CHAIN"
        # CH-A can use the shared fragment or its exact result; CH-B has only
        # the shared fragment.  The optimum reassigns CH-A to its exact row.
        handover = MaterialInput(
            "h.html",
            "<html><body>"
            "<h2>送检批次 CH-A</h2><p>站次 S01 2026-05-01 08:00—08:20</p>"
            "<table><tr><th>瓶号</th><th>采样时间</th></tr>"
            "<tr><td>S01-001</td><td>08:00</td></tr></table>"
            "<h2>送检批次 CH-B</h2><p>站次 S01 2026-05-01 08:00—08:20</p>"
            "<table><tr><th>瓶号</th><th>采样时间</th></tr>"
            "<tr><td>S01-002</td><td>08:00</td></tr></table>"
            "</body></html>",
            kind="handover",
            voyage=voyage,
        )
        lab = MaterialInput(
            "l.html",
            "<html><body><table><tr><th>瓶号</th><th>站次</th><th>盐度</th></tr>"
            "<tr><td>S01-00</td><td>S01</td><td>34.110</td></tr>"
            "<tr><td>S01-001</td><td>S01</td><td>34.220</td></tr>"
            "</table></body></html>",
            kind="lab",
            voyage=voyage,
        )
        self.service.import_materials([handover, lab])

        by_code = {
            b["batch_code"]: self.service.get_batch(b["id"])
            for b in self.service.list_batches(voyage=voyage)
        }
        self.assertEqual(by_code["CH-A"]["status"], "published")
        self.assertEqual(by_code["CH-B"]["status"], "published")
        salinities = {
            code: by_code[code]["results"][0]["salinity"] for code in by_code
        }
        self.assertEqual(salinities, {"CH-A": 34.22, "CH-B": 34.11})

    def test_twenty_four_independent_contentions_resolve_quickly(self) -> None:
        voyage = "TWENTY_FOUR"
        groups: list[tuple[str, str]] = []
        for index in range(24):
            station = f"T{index:02d}"
            groups.append((f"T-{index}-A", station))
            groups.append((f"T-{index}-B", station))
        self.service.import_materials([
            handover_material(voyage, groups),
            # Two results per station are alternatives for both duplicate
            # single-bottle batches in that independent contention group.
            MaterialInput(
                "lab.html",
                "<html><body><table><tr><th>瓶号</th><th>站次</th><th>盐度</th></tr>"
                + "".join(
                    f"<tr><td>T{index:02d}-001</td><td>T{index:02d}</td>"
                    f"<td>{30 + index / 100:.3f}</td></tr>"
                    f"<tr><td>T{index:02d}-001</td><td>T{index:02d}</td>"
                    f"<td>{35 + index / 100:.3f}</td></tr>"
                    for index in range(24)
                )
                + "</table></body></html>",
                kind="lab",
                voyage=voyage,
            ),
        ])

        batches = self.service.list_batches(voyage=voyage)
        self.assertEqual(len(batches), 48)
        self.assertEqual(sum(b["status"] == "in_review" for b in batches), 48)

        started = time.perf_counter()
        for index in range(24):
            code = f"T-{index}-A"
            items = [
                item for item in self.service.list_review_queue(
                    voyage=voyage, batch=code, status="open")["items"]
                if item["kind"] == "ambiguous_assignment"
            ]
            self.assertEqual(len(items), 1)
            self.assertEqual(len(items[0]["candidates"]), 2)
            self.service.decide(
                items[0]["id"], "confirm",
                value={"result": items[0]["candidates"][0]["result"]},
            )

        after = self.service.list_batches(voyage=voyage)
        self.assertEqual(sum(b["status"] == "published" for b in after), 48)
        self.assertEqual(
            sum(self.service.query_samples(batch=b["id"])["total"] for b in after),
            48,
        )
        # Keep the local stress case honest without making the suite flaky on
        # a heavily loaded CI machine; verify.py enforces the 80s budget.
        self.assertLess(time.perf_counter() - started, 60.0)


if __name__ == "__main__":
    unittest.main()
