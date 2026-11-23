"""Import runs: live progress, rollback position, interrupt/restart and rerun.

The scenario in the brief is a multi-file import that stalls in the batch
update stage: a caller querying /api/runs must see the current stage and the
last material that finished, a failure or kill must leave the stop position
visible while the sample sheet and review queue keep their pre-import state,
and a rerun must converge on the same result.  A run whose transaction was
committed but whose caller never got the response must still read as completed
after a restart.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
import unittest
from pathlib import Path

from helpers import FIXTURES, ROOT, VOYAGE, ServiceTestCase, fixtures, material

from app.config import Settings
from app.service import MaterialInput, SamplingService

PAUSE_ENV = ("SAMPLING_PAUSE_POINT", "SAMPLING_PAUSE_MARKER",
             "SAMPLING_PAUSE_TIMEOUT", "SAMPLING_FAIL_POINT")


class _Env:
    """Set environment variables for one test and restore them afterwards."""

    def __init__(self, case: unittest.TestCase, **values: str):
        self.case = case
        self.values = values

    def __enter__(self):
        self.prior = {key: os.environ.get(key) for key in PAUSE_ENV}
        for key in PAUSE_ENV:
            os.environ.pop(key, None)
        os.environ.update(self.values)
        return self

    def __exit__(self, *exc):
        for key, value in self.prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def wait_for(predicate, timeout: float = 5.0, interval: float = 0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def snapshot(service: SamplingService) -> dict:
    """Everything an interrupted import must leave untouched."""
    return {
        "materials": service.db.scalar(
            "SELECT COUNT(*) FROM materials WHERE status = 'active'", (), 0),
        "batches": {
            b["batch_code"]: (b["status"], b["version"])
            for b in service.list_batches(voyage=VOYAGE)
        },
        "samples": service.query_samples(voyage=VOYAGE, limit=1000)["total"],
        "review": sorted(
            (i["id"], i["status"]) for i in service.list_review_queue(limit=1000)["items"]),
    }


class ProgressDuringImportTests(ServiceTestCase):
    def test_runs_reports_stage_and_last_material_while_batches_are_updated(self) -> None:
        self.load()
        marker = Path(self.tmp.name) / "pause.json"
        result: dict = {}

        def run_import():
            try:
                result["reports"] = self.service.import_materials(
                    [material("lab_results_1.txt", "lab"),
                     material("lab_results_2.html", "lab")],
                    replace=True,
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                result["error"] = exc

        with _Env(self, SAMPLING_PAUSE_POINT="batches",
                  SAMPLING_PAUSE_MARKER=str(marker),
                  SAMPLING_PAUSE_TIMEOUT="10"):
            worker = threading.Thread(target=run_import)
            worker.start()
            self.assertIsNotNone(wait_for(marker.exists), "import never reached batches")
            # The import is holding the write transaction.  A status query
            # must still answer promptly and show where the import is.
            started = time.monotonic()
            runs = self.service.list_runs()
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 1.0)
            self.assertEqual(len(runs), 2)  # initial load + this rerun
            live = runs[0]
            self.assertEqual(live["status"], "running")
            self.assertEqual(live["phase"], "batches")
            self.assertEqual(live["phase_label"], "更新批次")
            self.assertEqual(live["last_completed_material"], "lab_results_2.html")
            self.assertEqual(live["materials_done"], 2)
            self.assertEqual(live["materials_total"], 2)
            marker.unlink()
            worker.join(10)
        self.assertNotIn("error", result)
        completed = self.service.get_run(live["id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["batches_done"], completed["batches_total"])
        self.assertGreater(completed["batches_total"], 0)
        self.assertEqual(completed["last_completed_material"], "lab_results_2.html")

    def test_http_runs_endpoint_shows_the_live_position(self) -> None:
        from app.api import make_server
        import urllib.request

        self.load()
        marker = Path(self.tmp.name) / "pause.json"
        server = make_server("127.0.0.1", 0, self.service)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        with _Env(self, SAMPLING_PAUSE_POINT="material",
                  SAMPLING_PAUSE_MARKER=str(marker),
                  SAMPLING_PAUSE_TIMEOUT="10"):
            worker = threading.Thread(target=lambda: self.service.import_materials(
                [material("deck_log.html", "deck"),
                 material("deck_log_2.txt", "deck")], replace=True))
            worker.start()
            self.assertIsNotNone(wait_for(marker.exists))
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/runs", timeout=5) as response:
                body = json.load(response)
            self.assertEqual(len(body["active"]), 1)
            active = body["active"][0]
            self.assertEqual(active["phase"], "materials")
            self.assertEqual(active["phase_label"], "摄取材料")
            self.assertEqual(active["last_completed_material"], "deck_log.html")
            self.assertIsNone(active["current_material"])
            self.assertEqual(active["materials_done"], 1)
            self.assertEqual(active["materials_total"], 2)
            marker.unlink()
            worker.join(10)

    def test_current_material_is_reported_while_it_is_being_read(self) -> None:
        self.load()
        marker = Path(self.tmp.name) / "pause.json"
        result: dict = {}

        def run_import():
            try:
                result["reports"] = self.service.import_materials(
                    [material("deck_log.html", "deck"),
                     material("deck_log_2.txt", "deck")], replace=True)
            except BaseException as exc:  # pragma: no cover - asserted below
                result["error"] = exc

        with _Env(self, SAMPLING_PAUSE_POINT="material-start",
                  SAMPLING_PAUSE_MARKER=str(marker),
                  SAMPLING_PAUSE_TIMEOUT="10"):
            worker = threading.Thread(target=run_import)
            worker.start()
            self.assertIsNotNone(wait_for(marker.exists))
            live = self.service.list_runs()[0]
            self.assertEqual(live["phase"], "materials")
            self.assertEqual(live["current_material"], "deck_log.html")
            self.assertEqual(live["materials_done"], 0)
            self.assertEqual(live["materials_total"], 2)
            marker.unlink()
            worker.join(10)
        self.assertNotIn("error", result)


class CrossProcessVisibilityTests(ServiceTestCase):
    """A separate process must be able to watch an import it is not running."""

    def test_status_process_starts_while_an_import_holds_the_write_lock(self) -> None:
        self.load()
        marker = Path(self.tmp.name) / "pause.json"
        result: dict = {}

        def run_import():
            try:
                result["reports"] = self.service.import_materials(
                    [material("lab_results_1.txt", "lab")], replace=True)
            except BaseException as exc:  # pragma: no cover - asserted below
                result["error"] = exc

        with _Env(self, SAMPLING_PAUSE_POINT="batches",
                  SAMPLING_PAUSE_MARKER=str(marker),
                  SAMPLING_PAUSE_TIMEOUT="10"):
            worker = threading.Thread(target=run_import)
            worker.start()
            self.assertIsNotNone(wait_for(marker.exists))
            # A fresh process (simulated by a fresh service over the same
            # database) must start without blocking on the import's lock and
            # must see the live position from the sidecar.
            started = time.monotonic()
            observer = SamplingService(settings=self.service.settings)
            self.addCleanup(observer.db.close)
            live = observer.list_runs()
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 2.0)
            self.assertEqual(live[0]["status"], "running")
            self.assertEqual(live[0]["phase"], "batches")
            marker.unlink()
            worker.join(10)
        self.assertNotIn("error", result)


class RollbackTests(ServiceTestCase):
    def test_failure_in_batch_stage_rolls_back_and_records_position(self) -> None:
        self.load()
        before = snapshot(self.service)
        with _Env(self, SAMPLING_FAIL_POINT="batches"):
            with self.assertRaises(RuntimeError):
                self.service.import_materials([
                    material("lab_results_1.txt", "lab",
                             (FIXTURES / "lab_results_1.txt").read_text("utf-8")
                             .replace("33.842", "33.999")),
                    material("lab_results_2.html", "lab",
                             (FIXTURES / "lab_results_2.html").read_text("utf-8")
                             .replace("33.997", "33.111")),
                ], replace=True)
        # Sample sheet and review queue are exactly as they were.
        self.assertEqual(snapshot(self.service), before)
        failed = self.service.list_runs()[0]
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["phase"], "failed")
        self.assertEqual(failed["last_completed_material"], "lab_results_2.html")
        self.assertEqual(failed["materials_done"], 2)
        self.assertEqual(failed["stopped_at"]["phase"], "batches")

    def test_failure_in_material_stage_keeps_earlier_material_out(self) -> None:
        self.load()
        before = snapshot(self.service)
        with _Env(self, SAMPLING_FAIL_POINT="materials"):
            with self.assertRaises(RuntimeError):
                self.service.import_materials([material("lab_results_1.txt", "lab")])
        self.assertEqual(snapshot(self.service), before)


CHILD = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, {root!r})
    sys.path.insert(0, {tests!r})
    from pathlib import Path
    from app.config import Settings
    from app.service import SamplingService
    from helpers import fixtures, FIXTURES

    service = SamplingService(settings=Settings(
        database_url={db!r}, fixture_dir=FIXTURES))
    service.import_materials(fixtures())
    """
)


def spawn_child(tmp: Path, point: str, marker: Path, db: str) -> subprocess.Popen:
    script = tmp / f"child_{point}.py"
    script.write_text(CHILD.format(root=str(ROOT), tests=str(ROOT / "tests"), db=db),
                      encoding="utf-8")
    env = dict(os.environ)
    env.update({
        "SAMPLING_PAUSE_POINT": point,
        "SAMPLING_PAUSE_MARKER": str(marker),
        "SAMPLING_PAUSE_TIMEOUT": "60",
        "PYTHONPATH": f"{ROOT}:{ROOT / 'tests'}",
    })
    return subprocess.Popen([sys.executable, "-I", str(script)], env=env)


class ProcessKillTests(ServiceTestCase):
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path}"

    def test_kill_in_batch_stage_leaves_pre_import_state_and_position(self) -> None:
        self.load()
        before = snapshot(self.service)
        self.service.db.close()  # let the child own the database file

        marker = Path(self.tmp.name) / "pause.json"
        child = spawn_child(Path(self.tmp.name), "batches", marker, self.db_url())
        try:
            self.assertIsNotNone(wait_for(marker.exists, timeout=30),
                                 "child never reached the batch stage")
            child.send_signal(signal.SIGKILL)
            child.wait(timeout=10)
        finally:
            if child.poll() is None:  # pragma: no cover - safety
                child.kill()

        reopened = SamplingService(settings=Settings(
            database_url=self.db_url(), fixture_dir=FIXTURES))
        self.addCleanup(reopened.db.close)
        # The stopped position is visible, including the last finished file.
        interrupted = reopened.list_runs()[0]
        self.assertEqual(interrupted["status"], "interrupted")
        self.assertEqual(interrupted["stopped_at"]["phase"], "batches")
        self.assertEqual(interrupted["stopped_at"]["last_completed_material"],
                         "lab_results_2.html")
        self.assertEqual(interrupted["materials_done"], 5)
        # No half-applied import: sample sheet and review queue as before.
        self.assertEqual(snapshot(reopened), before)

        # A rerun converges on the documented end state.
        reopened.import_materials(fixtures())
        batches = {b["batch_code"]: b["status"] for b in reopened.list_batches(voyage=VOYAGE)}
        self.assertEqual(batches["KX-2603-A"], "published")
        self.assertEqual(batches["KX-2603-E"], "held")
        self.assertEqual(batches["KX-2603-G"], "in_review")
        runs = reopened.list_runs()
        self.assertEqual(len(runs), 3)  # initial load, killed run, rerun
        self.assertEqual(runs[0]["status"], "completed")
        self.assertEqual(runs[1]["status"], "interrupted")
        self.assertEqual(runs[2]["status"], "completed")

    def test_kill_after_commit_still_reads_completed_after_restart(self) -> None:
        # The caller never receives the response, but the whole import was
        # committed: on restart the run must not be reported as interrupted.
        marker = Path(self.tmp.name) / "pause.json"
        child = spawn_child(Path(self.tmp.name), "after-commit", marker, self.db_url())
        try:
            self.assertIsNotNone(wait_for(marker.exists, timeout=30),
                                 "child never committed the import")
            child.send_signal(signal.SIGKILL)
            child.wait(timeout=10)
        finally:
            if child.poll() is None:  # pragma: no cover - safety
                child.kill()

        reopened = SamplingService(settings=Settings(
            database_url=self.db_url(), fixture_dir=FIXTURES))
        self.addCleanup(reopened.db.close)
        runs = reopened.list_runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "completed")
        self.assertEqual(runs[0]["phase"], "completed")
        self.assertFalse(runs[0]["in_flight"])
        self.assertEqual(len(runs[0]["summary"]), 5)
        self.assertEqual(reopened.query_samples(batch="KX-2603-A")["total"], 4)
        # Rerunning is a no-op rather than a duplicate import.
        again = reopened.import_materials(fixtures())
        self.assertTrue(all(r.unchanged for r in again))


if __name__ == "__main__":
    unittest.main()
