"""Offline verification for the voyage daily report.

One command checks three things that are easy to get wrong and expensive to
discover in production:

1. **Values.**  Every ``(voyage, station, sampling day)`` count and the exact
   salinity median agree with an independent pass over the sample tables --
   before and after an import, a reviewer confirmation, a value revision and a
   withdrawal.  Bottles of a batch that is only set aside are still counted.
2. **Rollback and retry.**  A run that fails after publishing rolls the report
   back with the rest of the transaction, and retrying it counts every bottle
   exactly once.
3. **Cost at 100 000 bottles in one group.**  Reading a summary and applying a
   single-bottle change are each measured in SQLite virtual-machine steps, and
   the application-side row loads are counted, proving neither path scans the
   group's samples.

Run it with::

    python3 run.py daily-check          # or: python3 -m app.dailycheck
"""

from __future__ import annotations

import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import Settings
from .daily import _median_of
from .service import MaterialInput, SamplingService

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"
VOYAGE = "KX-26-03"
BIG_BOTTLES = 100_000
BIG_DISTINCT = 1_000
STEP_BUDGET = 50_000
ROW_BUDGET = 64

FIXTURE_FILES = (
    ("deck_log.html", "deck"),
    ("deck_log_2.txt", "deck"),
    ("handover.html", "handover"),
    ("lab_results_1.txt", "lab"),
    ("lab_results_2.html", "lab"),
)


# ---------------------------------------------------------------------------
# Measurement helpers
# ---------------------------------------------------------------------------

class StepCounter:
    """Count SQLite virtual-machine instructions issued on one connection."""

    def __init__(self, service: SamplingService):
        self.service = service
        self.steps = 0
        self._conn = service.db._conn

    def _tick(self) -> int:
        self.steps += 1
        return 0

    @contextmanager
    def measure(self) -> Iterator["StepCounter"]:
        self.steps = 0
        self._conn.set_progress_handler(self._tick, 1)
        try:
            yield self
        finally:
            self._conn.set_progress_handler(None, 0)

    @contextmanager
    def count_rows(self) -> Iterator[list[int]]:
        """Count rows handed to the application through the DB wrapper."""
        loads = [0, 0]  # calls, rows
        original = self.service.db.fetchall

        def counting(query: str, params=()):
            rows = original(query, params)
            loads[0] += 1
            loads[1] += len(rows)
            return rows

        self.service.db.fetchall = counting  # type: ignore[method-assign]
        try:
            yield loads
        finally:
            self.service.db.fetchall = original  # type: ignore[method-assign]


class CheckFailure(Exception):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailure(message)


# ---------------------------------------------------------------------------
# 1. Values
# ---------------------------------------------------------------------------

def expected_report(service: SamplingService, voyage: str | None = None) -> dict:
    """Independent expectation built from the sample tables, not the ledger."""
    scan = service.daily.scan(voyage)
    return {
        (group[0], group[1], group[2]): {
            "bottle_count": entry["bottle_count"],
            "published_count": entry["published_count"],
            "salinity_count": entry["salinity_count"],
            "salinity_median": entry["salinity_median"],
        }
        for group, entry in scan.items()
    }


def compare(service: SamplingService, label: str, voyage: str | None = VOYAGE) -> list[str]:
    report = service.daily_report(voyage=voyage)
    actual = {
        (item["voyage"], item["station"], item["sample_day"]): {
            "bottle_count": item["bottle_count"],
            "published_count": item["published_count"],
            "salinity_count": item["salinity_count"],
            "salinity_median": item["salinity_median"],
        }
        for item in report["items"]
    }
    expected = expected_report(service, voyage)
    problems = []
    for group in sorted(set(actual) | set(expected)):
        if actual.get(group) != expected.get(group):
            problems.append(f"{label}: {group} report={actual.get(group)} "
                            f"expected={expected.get(group)}")
    return problems


def verify_values(service: SamplingService, lines: list[str]) -> list[str]:
    problems = compare(service, "after import")
    by_station = {(i["station"], i["sample_day"]): i for i in service.daily_report(
        voyage=VOYAGE)["items"]}
    # A held batch's registered bottles still count; none of them is published.
    held = by_station.get(("S05", "2026-03-04"))
    check(held is not None, "S05 group missing from the daily report")
    if held["bottle_count"] != 2 or held["published_count"] != 0:
        problems.append(f"held batch S05: {held}")
    # An in-review batch publishes only the invariant rows; the median is over
    # published values only.
    review = by_station.get(("S07", "2026-03-05"))
    if review is None or review["bottle_count"] != 4 or review["published_count"] != 2:
        problems.append(f"in-review batch S07: {review}")
    # Even-sized median is the exact mean of the two middle values.
    even = by_station.get(("S03", "2026-03-02"))
    if even is None or even["salinity_median"] != 33.8675:
        problems.append(f"even-sized median S03: {even}")
    lines.append(
        "   import        groups={groups} bottles={bottles} published={published}"
        .format(groups=len(by_station),
                bottles=sum(i["bottle_count"] for i in by_station.values()),
                published=sum(i["published_count"] for i in by_station.values())))

    # A reviewer confirmation publishes two more bottles of S07.
    queue = service.list_review_queue(status="open", voyage=VOYAGE)["items"]
    item = next(i for i in queue if i["kind"] == "ambiguous_assignment")
    service.decide(item["id"], "confirm", reviewer="verify",
                   value={"result": item["candidates"][0]["result"]})
    problems += compare(service, "after review")
    lines.append("   review        S07 published={} median={}".format(
        *(lambda row: (row["published_count"], row["salinity_median"]))(
            next(i for i in service.daily_report(voyage=VOYAGE)["items"]
                 if i["station"] == "S07"))))

    # Revising a measured value changes the median without changing counts.
    before_revision = next(i for i in service.daily_report(voyage=VOYAGE)["items"]
                           if i["station"] == "S01")["salinity_median"]
    original = (FIXTURES / "lab_results_1.txt").read_text(encoding="utf-8")
    service.revise_material("lab_results_1", original.replace("33.867", "30.000"))
    problems += compare(service, "after revision")
    s01 = next(i for i in service.daily_report(voyage=VOYAGE)["items"]
               if i["station"] == "S01")
    if s01["salinity_median"] != 33.9235:
        problems.append(f"revision did not reach the median: {s01}")
    lines.append("   revision      S01 median {} -> {} after 33.867 -> 30.000".format(
        before_revision, s01["salinity_median"]))

    # Withdrawing a laboratory report unpublishes its batches' values but the
    # registered bottles remain counted.
    service.withdraw_material("lab_results_2")
    problems += compare(service, "after withdrawal")
    cleared = next(i for i in service.daily_report(voyage=VOYAGE)["items"]
                   if i["station"] == "S07")
    if (cleared["bottle_count"], cleared["published_count"],
            cleared["salinity_median"]) != (4, 0, None):
        problems.append(f"withdrawal did not clear S07: {cleared}")
    lines.append("   withdrawal    S07 bottles=4 published=0 median=None")
    return problems


# ---------------------------------------------------------------------------
# 2. Rollback and retry
# ---------------------------------------------------------------------------

class InjectedFailure(Exception):
    pass


def verify_retry(service: SamplingService, lines: list[str]) -> list[str]:
    problems = []
    before = service.daily_report(voyage=VOYAGE)

    revised = (FIXTURES / "lab_results_2.html").read_text(encoding="utf-8")
    revised = revised.replace("<td>—</td>", "<td>S05-002</td>")
    material = MaterialInput(name="lab_results_2.html", content=revised,
                             kind="lab", voyage=VOYAGE)

    # The outer transaction fails after the import has published its batches;
    # the report must roll back with them.
    try:
        with service.db.transaction():
            service.import_materials([material], trigger="verify-fail")
            raise InjectedFailure("injected failure after publishing")
    except InjectedFailure:
        pass
    after_fail = service.daily_report(voyage=VOYAGE)
    if after_fail != before:
        problems.append("report changed after a rolled-back run")
    if service.daily_report_drift(VOYAGE):
        problems.append("report drifted from the samples after a rolled-back run")
    held = next(i for i in after_fail["items"] if i["station"] == "S05")
    if held["bottle_count"] != 2 or held["published_count"] != 0:
        problems.append(f"rolled-back run left S05 changed: {held}")
    lines.append("   failed run    rolled back; S05 remains held, report unchanged")

    # Retrying the same change succeeds and counts each bottle once.
    service.import_materials([material], trigger="verify-retry")
    problems += compare(service, "after retry")
    once = service.daily_report(voyage=VOYAGE)
    s05 = next(i for i in once["items"] if i["station"] == "S05")
    if (s05["bottle_count"], s05["published_count"]) != (2, 2):
        problems.append(f"retry produced S05 {s05}")
    # Replaying the identical import must change nothing (no double count).
    service.import_materials([material], trigger="verify-retry")
    twice = service.daily_report(voyage=VOYAGE)
    if twice != once:
        problems.append("replaying an identical import changed the report")
    lines.append("   retry         S05 published=2; identical replay is a no-op")
    return problems


# ---------------------------------------------------------------------------
# 3. Cost
# ---------------------------------------------------------------------------

def build_large_group(service: SamplingService) -> str:
    ledger = service.daily
    voyage = "SCALE"
    batch_id = "scale_batch"
    bottles = [
        {"id": f"scale-{index}",
         "station": "S99",
         "sampled_at": "2026-06-01T08:00"}
        for index in range(BIG_BOTTLES)
    ]
    published = {
        bottle["id"]: 30.0 + (index % BIG_DISTINCT) / 1000.0
        for index, bottle in enumerate(bottles)
    }
    # One transaction, as in production: this is fixture construction, not a
    # measurement.
    with service.db.transaction():
        ledger.register_batch(batch_id, voyage, "S99", bottles)
        ledger.set_bottles(batch_id, published)
    return voyage


def verify_cost(lines: list[str]) -> list[str]:
    problems = []
    with tempfile.TemporaryDirectory() as tmp:
        service = SamplingService(settings=Settings(
            database_url=f"sqlite:///{Path(tmp) / 'scale.db'}",
            fixture_dir=FIXTURES,
        ))
        try:
            started = time.time()
            voyage = build_large_group(service)
            built = time.time() - started
            counter = StepCounter(service)

            with counter.measure():
                report = service.daily_report(voyage=voyage, station="S99")
            read_steps = counter.steps
            with counter.count_rows() as read_rows:
                service.daily_report(voyage=voyage, station="S99")
            if read_steps > STEP_BUDGET:
                problems.append(f"summary read used {read_steps} VM steps")
            if read_rows[1] > ROW_BUDGET:
                problems.append(f"summary read loaded {read_rows[1]} rows")

            group = report["items"][0]
            values = [30.0 + (index % BIG_DISTINCT) / 1000.0
                      for index in range(BIG_BOTTLES)]
            values.sort()
            expected = _median_of(values[(len(values) - 1) // 2],
                                  values[len(values) // 2], len(values) % 2 == 1)
            if group["bottle_count"] != BIG_BOTTLES:
                problems.append(f"large group bottle_count={group['bottle_count']}")
            if group["salinity_median"] != expected:
                problems.append(
                    f"large group median={group['salinity_median']!r} expected={expected!r}")

            victim = "scale-50000"
            with counter.measure():
                service.daily.set_bottle(victim, False, None)
            change_steps = counter.steps
            with counter.count_rows() as change_rows:
                service.daily.set_bottle(victim, True,
                                         30.0 + (50000 % BIG_DISTINCT) / 1000.0)
            if change_steps > STEP_BUDGET:
                problems.append(f"single-bottle change used {change_steps} VM steps")
            if change_rows[1] > ROW_BUDGET:
                problems.append(f"single-bottle change loaded {change_rows[1]} rows")

            restored = service.daily_report(voyage=voyage, station="S99")["items"][0]
            if restored["bottle_count"] != BIG_BOTTLES:
                problems.append("re-publish changed the bottle count")
            if restored["salinity_median"] != expected:
                problems.append("re-publish did not restore the median")

            lines.append(
                f"   group         {BIG_BOTTLES:,} bottles / {BIG_DISTINCT:,} distinct "
                f"values (built in {built:.1f}s)")
            lines.append(
                f"   summary read  {read_steps:>6,} VM steps   "
                f"{read_rows[0]} queries, {read_rows[1]} rows loaded")
            lines.append(
                f"   one change    {change_steps:>6,} VM steps   "
                f"{change_rows[0]} queries, {change_rows[1]} rows loaded")
            lines.append(f"   budgets       {STEP_BUDGET:,} VM steps each, "
                         f"{ROW_BUDGET} rows")
        finally:
            service.db.close()
    return problems


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def run() -> int:
    print("=" * 72)
    print("Voyage daily report — offline verification")
    print("=" * 72)
    problems: list[str] = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            service = SamplingService(settings=Settings(
                database_url=f"sqlite:///{Path(tmp) / 'values.db'}",
                fixture_dir=FIXTURES,
            ))
            try:
                service.import_materials([
                    MaterialInput(name=name,
                                  content=(FIXTURES / name).read_text(encoding="utf-8"),
                                  kind=kind, voyage=VOYAGE)
                    for name, kind in FIXTURE_FILES
                ])
                print("\n1. Values follow the published samples")
                lines: list[str] = []
                problems += verify_values(service, lines)
                for line in lines:
                    print(line)
                print("\n2. Rollback and retry")
                lines = []
                problems += verify_retry(service, lines)
                for line in lines:
                    print(line)
            finally:
                service.db.close()
        print("\n3. Cost at scale")
        lines = []
        problems += verify_cost(lines)
        for line in lines:
            print(line)
    except CheckFailure as exc:
        problems.append(str(exc))
    print()
    if problems:
        print("FAIL")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("OK: values exact, rollback/retry safe, summaries stay within budget")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
