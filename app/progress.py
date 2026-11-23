"""Out-of-transaction progress for import runs.

An import is one database transaction: either the whole batch of materials is
applied or none of it is.  That is exactly what keeps the sample tables, the
review queue and the batch versions consistent after a failure, but it also
means the ``runs`` row cannot be updated while the import is in flight -- a
write on the same connection would be part of the transaction and would
disappear along with it.

This module keeps the live position in a small sidecar store instead:

* in memory, always, so ``GET /api/runs`` in the serving process can answer
  without touching the (write-locked) main connection;
* optionally in a JSON file next to the SQLite database, so a separate
  process (a CLI import) is observable too, and so the position survives an
  abrupt process kill for the next startup to report.

The sidecar is a diagnostic channel only.  Nothing in the sample/batch/review
tables is derived from it.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from .util import from_json, to_json

# Phase names are stable machine values; labels are for humans reading the
# run list.  "batches" is the stage where the whole voyage is re-resolved and
# batch versions / published rows / review items are rewritten.
PHASE_LABELS = {
    "started": "准备导入",
    "materials": "摄取材料",
    "batches": "更新批次",
    "completed": "完成",
    "failed": "失败",
    "interrupted": "已中断",
}

MAX_KEPT = 32


def progress_path_for(database_url: str) -> Path | None:
    """Where to mirror progress so other processes can see it.

    Embedded SQLite keeps the file next to the database.  PostgreSQL has no
    adjacent file, so an explicit ``SAMPLING_PROGRESS_FILE`` opts in; without
    it progress is process-local (the API process that runs the import still
    sees it).
    """
    if database_url.startswith("sqlite:"):
        raw = database_url.split("sqlite://", 1)[-1]
        if raw.startswith("//"):
            raw = raw[1:]
        if not raw or raw == ":memory:":
            return None
        return Path(raw + ".progress.json")
    explicit = os.environ.get("SAMPLING_PROGRESS_FILE")
    return Path(explicit) if explicit else None


class ProgressStore:
    """Thread-safe, crash-tolerant store for in-flight run positions."""

    def __init__(self, path: Path | None = None):
        self.path = path
        self._lock = threading.RLock()
        self._records: dict[str, dict[str, Any]] = {}
        self._load()

    # -- file mirror -----------------------------------------------------
    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            data = from_json(self.path.read_text(encoding="utf-8"), {})
        except OSError:
            return
        if isinstance(data, dict):
            for run_id, record in (data.get("runs") or {}).items():
                if isinstance(record, dict):
                    record.setdefault("id", run_id)
                    self._records[run_id] = record

    def _save(self) -> None:
        if self.path is None:
            return
        keep = sorted(
            self._records.values(),
            key=lambda r: r.get("updated_at") or "",
            reverse=True,
        )[:MAX_KEPT]
        payload = {"runs": {r["id"]: r for r in keep}}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            # Observability must never break an import.
            pass

    # -- records ---------------------------------------------------------
    def start(self, run_id: str, trigger: str, materials_total: int,
              seq: int = 0) -> dict[str, Any]:
        stamp = _stamp()
        record = {
            "id": run_id,
            "seq": seq,
            "trigger": trigger,
            "status": "running",
            "phase": "started",
            "phase_label": PHASE_LABELS["started"],
            "current_material": None,
            "last_completed_material": None,
            "materials_total": materials_total,
            "materials_done": 0,
            "batches_total": 0,
            "batches_done": 0,
            "started_at": stamp,
            "updated_at": stamp,
            "pid": os.getpid(),
        }
        with self._lock:
            self._records[run_id] = record
            self._save()
        return dict(record)

    def update(self, run_id: str, **fields: Any) -> dict[str, Any] | None:
        with self._lock:
            record = self._records.get(run_id)
            if record is None:
                return None
            if "phase" in fields:
                fields.setdefault("phase_label", PHASE_LABELS.get(fields["phase"], fields["phase"]))
            record.update(fields)
            record["updated_at"] = _stamp()
            self._save()
            return dict(record)

    def finish(self, run_id: str, status: str,
               summary: Any = None) -> dict[str, Any] | None:
        with self._lock:
            record = self._records.get(run_id)
            if record is None:
                return None
            record.update({
                "status": status,
                "phase": status,
                "phase_label": PHASE_LABELS.get(status, status),
                "current_material": None,
                "finished_at": _stamp(),
                "updated_at": _stamp(),
            })
            if summary is not None:
                record["summary"] = summary
            self._save()
            return dict(record)

    def refresh(self) -> None:
        """Pick up positions written by another process.

        The in-memory record wins whenever it is at least as new as the mirror,
        so a process that is itself running an import never reads its own
        position backwards from a stale file.
        """
        with self._lock:
            prior = dict(self._records)
            self._records = {}
            self._load()
            for run_id, record in prior.items():
                current = self._records.get(run_id)
                if current is None or (record.get("updated_at") or "") >= (
                        current.get("updated_at") or ""):
                    self._records[run_id] = record

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._records.get(run_id)
            return dict(record) if record else None

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._records.values()]

    def active(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._records.values() if r.get("status") == "running"]

    def drop(self, run_id: str) -> None:
        with self._lock:
            if self._records.pop(run_id, None) is not None:
                self._save()

    def retain(self, run_id: str, record: dict[str, Any]) -> None:
        """Replace an entry with its final (not running) form."""
        with self._lock:
            self._records[run_id] = dict(record)
            self._save()


def _stamp() -> str:
    # Local import keeps this module independent of the service package.
    from .util import now_iso

    return now_iso()


def wait_for_genuine_marker(marker: Path, token: str, timeout: float) -> None:
    """Block while *marker* still holds *token*.

    The test harness deletes (or replaces) the marker to release a pause.  A
    marker left behind by an earlier, interrupted run has a different token
    and is therefore ignored, so a rerun never stalls on stale harness state.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data = from_json(marker.read_text(encoding="utf-8"), {})
        except OSError:
            return
        if not isinstance(data, dict) or data.get("token") != token:
            return
        time.sleep(0.02)


def write_marker(marker: Path, record: dict[str, Any]) -> None:
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        tmp = marker.with_name(marker.name + ".tmp")
        tmp.write_text(to_json(record), encoding="utf-8")
        os.replace(tmp, marker)
    except OSError:
        pass
