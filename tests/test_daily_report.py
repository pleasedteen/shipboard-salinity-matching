"""Voyage daily report: counts, exact median, idempotency and cost."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from helpers import FIXTURES, ServiceTestCase

from app.config import Settings
from app.daily import _median_of
from app.service import SamplingService


class DailyReportValueTests(ServiceTestCase):
    def test_report_after_import(self) -> None:
        self.load()
        report = self.service.daily_report(voyage="KX-26-03")
        by_group = {(i["station"], i["sample_day"]): i for i in report["items"]}
        # A held batch's bottles are still on the register.
        held = by_group[("S05", "2026-03-04")]
        self.assertEqual(held["bottle_count"], 2)
        self.assertEqual(held["published_count"], 0)
        self.assertIsNone(held["salinity_median"])
        self.assertEqual(held["salinity_count"], 0)
        # An in-review batch publishes only its invariant pairings.
        review = by_group[("S07", "2026-03-05")]
        self.assertEqual((review["bottle_count"], review["published_count"]), (4, 2))
        # Two batches on the same station and day fall into one group.
        s01 = by_group[("S01", "2026-03-01")]
        self.assertEqual(s01["bottle_count"], 6)
        self.assertEqual(s01["published_count"], 6)
        self.assertEqual(report["bottle_count"], 22)
        self.assertEqual(report["published_count"], 18)

    def test_median_is_exact_for_even_and_odd_counts(self) -> None:
        self.load()
        by_group = {(i["station"], i["sample_day"]): i
                    for i in self.service.daily_report(voyage="KX-26-03")["items"]}
        # S03: 33.775, 33.960 -> mean of the two middle values.
        self.assertEqual(by_group[("S03", "2026-03-02")]["salinity_median"], 33.8675)
        # S02: 33.901, 34.024, 34.077 -> middle value.
        self.assertEqual(by_group[("S02", "2026-03-01")]["salinity_median"], 34.024)

    def test_report_matches_samples_after_every_lifecycle_step(self) -> None:
        self.load()
        self.assertEqual(self.service.daily_report_drift("KX-26-03"), [])

        item = self.open_items("KX-2603-G", "ambiguous_assignment")[0]
        self.service.decide(item["id"], "confirm", reviewer="张敏",
                            value={"result": item["candidates"][0]["result"]})
        self.assertEqual(self.service.daily_report_drift("KX-26-03"), [])
        s07 = self.group("S07")
        self.assertEqual((s07["bottle_count"], s07["published_count"]), (4, 4))
        self.assertEqual(s07["salinity_median"], 33.9555)

        original = (FIXTURES / "lab_results_1.txt").read_text(encoding="utf-8")
        self.service.revise_material("lab_results_1", original.replace("33.842", "30.000"))
        self.assertEqual(self.service.daily_report_drift("KX-26-03"), [])
        self.assertEqual(self.group("S01")["salinity_median"], 33.936)

        self.service.withdraw_material("lab_results_2")
        self.assertEqual(self.service.daily_report_drift("KX-26-03"), [])
        after = self.group("S07")
        self.assertEqual((after["bottle_count"], after["published_count"],
                          after["salinity_median"]), (4, 0, None))

    def group(self, station: str, day: str | None = None) -> dict:
        groups = {(i["station"], i["sample_day"]): i
                  for i in self.service.daily_report(voyage="KX-26-03")["items"]}
        if day is None:
            day = next(key[1] for key in groups if key[0] == station)
        return groups[(station, day)]


class DailyReportIdempotencyTests(ServiceTestCase):
    def test_reimport_and_identical_replay_do_not_move_counters(self) -> None:
        self.load()
        before = self.service.daily_report(voyage="KX-26-03")
        self.service.import_materials(self._fixtures_again())
        self.assertEqual(self.service.daily_report(voyage="KX-26-03"), before)

    def test_failed_import_rolls_the_report_back_and_retry_counts_once(self) -> None:
        self.load()
        before = self.service.daily_report(voyage="KX-26-03")
        revised = (FIXTURES / "lab_results_2.html").read_text(encoding="utf-8")
        material = self._material(revised.replace("<td>—</td>", "<td>S05-002</td>"))

        class Boom(Exception):
            pass

        with self.assertRaises(Boom):
            with self.service.db.transaction():
                self.service.import_materials([material], trigger="fail")
                raise Boom()
        self.assertEqual(self.service.daily_report(voyage="KX-26-03"), before)
        self.assertEqual(self.service.daily_report_drift("KX-26-03"), [])

        self.service.import_materials([material], trigger="retry")
        once = self.service.daily_report(voyage="KX-26-03")
        s05 = next(i for i in once["items"] if i["station"] == "S05")
        self.assertEqual((s05["bottle_count"], s05["published_count"]), (2, 2))
        # Replaying the identical import (already `unchanged`) counts nothing.
        self.service.import_materials([material], trigger="retry")
        self.assertEqual(self.service.daily_report(voyage="KX-26-03"), once)

    def test_rebuild_reproduces_the_incremental_report(self) -> None:
        self.load()
        item = self.open_items("KX-2603-G", "ambiguous_assignment")[0]
        self.service.decide(item["id"], "confirm",
                            value={"result": item["candidates"][0]["result"]})
        before = self.service.daily_report(voyage="KX-26-03")
        self.service.rebuild_daily_report("KX-26-03")
        after = self.service.daily_report(voyage="KX-26-03")
        self.assertEqual(
            [(i["station"], i["sample_day"], i["bottle_count"], i["published_count"],
              i["salinity_count"], i["salinity_median"]) for i in before["items"]],
            [(i["station"], i["sample_day"], i["bottle_count"], i["published_count"],
              i["salinity_count"], i["salinity_median"]) for i in after["items"]],
        )

    @staticmethod
    def _fixtures_again():
        from helpers import fixtures

        return fixtures()

    @staticmethod
    def _material(content: str):
        from app.service import MaterialInput

        return MaterialInput(name="lab_results_2.html", content=content,
                             kind="lab", voyage="KX-26-03")


class DailyReportCostTests(unittest.TestCase):
    """A 100 000-bottle group must stay cheap to read and to adjust."""

    BOTTLES = 100_000
    DISTINCT = 1_000
    STEP_BUDGET = 50_000
    ROW_BUDGET = 64

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.service = SamplingService(settings=Settings(
            database_url=f"sqlite:///{Path(cls.tmp.name) / 'scale.db'}",
            fixture_dir=FIXTURES,
        ))
        bottles = [{"id": f"b{index}", "station": "S99",
                    "sampled_at": "2026-06-01T08:00"} for index in range(cls.BOTTLES)]
        published = {bottle["id"]: 30.0 + (index % cls.DISTINCT) / 1000.0
                     for index, bottle in enumerate(bottles)}
        with cls.service.db.transaction():
            cls.service.daily.register_batch("scale", "SCALE", "S99", bottles)
            cls.service.daily.set_bottles("scale", published)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.service.db.close()
        cls.tmp.cleanup()

    def measure(self, action):
        connection = self.service.db._conn
        steps = [0]

        def tick() -> int:
            steps[0] += 1
            return 0

        loads = [0]
        original = self.service.db.fetchall

        def counting(query, params=()):
            rows = original(query, params)
            loads[0] += len(rows)
            return rows

        connection.set_progress_handler(tick, 1)
        self.service.db.fetchall = counting
        try:
            result = action()
        finally:
            connection.set_progress_handler(None, 0)
            self.service.db.fetchall = original
        return result, steps[0], loads[0]

    def test_summary_read_is_a_key_lookup(self) -> None:
        started = time.time()
        report, steps, rows = self.measure(
            lambda: self.service.daily_report(voyage="SCALE", station="S99"))
        self.assertLess(time.time() - started, 5)
        self.assertEqual(report["items"][0]["bottle_count"], self.BOTTLES)
        self.assertLess(steps, self.STEP_BUDGET)
        self.assertLessEqual(rows, self.ROW_BUDGET)

    def test_single_bottle_change_is_constant_cost(self) -> None:
        victim = "b50000"
        value = 30.0 + (50000 % self.DISTINCT) / 1000.0
        _, steps, rows = self.measure(
            lambda: self.service.daily.set_bottle(victim, False, None))
        self.assertLess(steps, self.STEP_BUDGET)
        self.assertLessEqual(rows, self.ROW_BUDGET)
        group = self.service.daily_report(voyage="SCALE", station="S99")["items"][0]
        self.assertEqual(group["bottle_count"], self.BOTTLES)
        self.assertEqual(group["published_count"], self.BOTTLES - 1)

        _, steps, rows = self.measure(
            lambda: self.service.daily.set_bottle(victim, True, value))
        self.assertLess(steps, self.STEP_BUDGET)
        self.assertLessEqual(rows, self.ROW_BUDGET)
        group = self.service.daily_report(voyage="SCALE", station="S99")["items"][0]
        values = sorted(30.0 + (index % self.DISTINCT) / 1000.0
                        for index in range(self.BOTTLES))
        self.assertEqual(
            group["salinity_median"],
            _median_of(values[(self.BOTTLES - 1) // 2], values[self.BOTTLES // 2],
                       self.BOTTLES % 2 == 1))


class DailyReportApiTests(ServiceTestCase):
    def test_http_endpoint_returns_the_summaries(self) -> None:
        import json
        import threading
        import urllib.request

        from app.api import make_server

        self.load()
        server = make_server("127.0.0.1", 0, self.service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/daily?voyage=KX-26-03&station=S05",
                timeout=10) as response:
            body = json.load(response)
        self.assertEqual(body["total_groups"], 1)
        item = body["items"][0]
        self.assertEqual((item["station"], item["bottle_count"],
                          item["published_count"]), ("S05", 2, 0))


class DailyReportUpgradeTests(unittest.TestCase):
    def test_existing_database_is_backfilled_once(self) -> None:
        """A database written before the report existed gets populated on open."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "legacy.db"
        settings = Settings(database_url=f"sqlite:///{path}", fixture_dir=FIXTURES)
        service = SamplingService(settings=settings)
        from helpers import fixtures

        service.import_materials(fixtures())
        before = service.daily_report(voyage="KX-26-03")
        # Simulate the state a pre-report version leaves behind.
        service.db.execute("DELETE FROM daily_reports")
        service.db.execute("DELETE FROM daily_bottle_facts")
        service.db.execute("DELETE FROM daily_salinity_buckets")
        service.db.execute("DELETE FROM daily_batches")
        service.db.close()

        reopened = SamplingService(settings=settings)
        self.addCleanup(reopened.db.close)
        self.assertEqual(reopened.daily_report(voyage="KX-26-03"), before)


if __name__ == "__main__":
    unittest.main()
