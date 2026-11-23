"""Incrementally maintained voyage daily report.

The report answers, for every ``(voyage, station, sampling day)`` group:

* how many bottles the current handover register holds -- bottles of a batch
  that is only set aside because its laboratory report cannot be paired still
  count;
* how many of them have a published salinity (``auto`` or ``confirmed``);
* the *exact* median of those published salinity values.

Both the sample tables and this report are written in the same transaction, so
a reader never sees a report that disagrees with the published samples.

Maintenance is delta based
--------------------------
``daily_bottle_facts`` holds one row per registered bottle.  Every write path
reports the old and the new state of the bottles it actually touched;
:class:`DailyLedger` applies the difference to the group counters and to an
indexed salinity bucket table.  Replaying the same state adds zero, which is
what makes a retry after a rolled-back run safe: counters are never recomputed
by summing samples, so a repeated request cannot be counted twice.

The median cursor
-----------------
A positive integer rank over the multiset of published salinity values is kept
as a pointer into the bucket table (the value, and how many values are strictly
below it).  Changing one bottle moves the pointer by at most one bucket, so
maintaining a group of 100 000 bottles costs a handful of primary-key and
index lookups -- it never walks the group's samples, and the application side
never loads them.  Reading a summary is one primary-key lookup.

Bulk changes (a first import, or a revision that rewrites a whole group) use a
single ordered window aggregate instead of moving the cursor step by step.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from decimal import Decimal
from typing import Any, Iterable

from .db import Database
from .util import normalize_station, now_iso, short_id

# A bottle without a stated station or sampling day is kept under an empty
# string rather than dropped, so the register total stays complete.
UNKNOWN = ""

# Above this many distinct values changed in one group, rebuilding the median
# from one ordered aggregate is cheaper than walking the cursor bucket by
# bucket.
CURSOR_WALK_LIMIT = 32

Summary = dict[str, Any]


def sample_day(value: str | None) -> str:
    """Return the sampling day (``YYYY-MM-DD``) of a timestamp, or ``''``."""
    if not value:
        return UNKNOWN
    text = str(value).strip()
    text = text.split("T", 1)[0] if "T" in text else text.split(" ", 1)[0]
    return text[:10] if len(text) >= 10 else (text or UNKNOWN)


def bottle_id_for(batch_id: str, record_key: str) -> str:
    return short_id("bottle", batch_id, record_key)


def group_key(voyage: str | None, station: str | None, day: str | None) -> tuple[str, str, str]:
    return (voyage or UNKNOWN, normalize_station(station) or UNKNOWN, sample_day(day))


def _salinity_key(value: float) -> str:
    """Order-preserving text key for a salinity value.

    Salinity is non-negative in practice; the sign prefix keeps the ordering
    stable even if a negative value is ever recorded, and the fixed number of
    decimals keeps equal values in one bucket.
    """
    return f"{float(value):+.10f}"


def _median_of(lower: float, upper: float, odd: bool) -> float:
    if odd:
        return float(lower)
    # Decimal avoids the binary rounding of (a + b) / 2 while keeping the
    # result a plain JSON number.
    return float((Decimal(repr(lower)) + Decimal(repr(upper))) / 2)


def _public(row: dict[str, Any]) -> Summary:
    return {
        "voyage": row["voyage"],
        "station": row["station"],
        "sample_day": row["sample_day"],
        "bottle_count": int(row["bottle_count"] or 0),
        "published_count": int(row["published_count"] or 0),
        "salinity_count": int(row["salinity_count"] or 0),
        "salinity_median": row["median_value"],
        "updated_at": row["updated_at"],
    }


class _GroupChange:
    """Differences to apply to one group."""

    __slots__ = ("bottles", "published", "salinities")

    def __init__(self) -> None:
        self.bottles = 0
        self.published = 0
        self.salinities: dict[float, int] = defaultdict(int)

    def empty(self) -> bool:
        return self.bottles == 0 and self.published == 0 and not any(self.salinities.values())


class DailyLedger:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------
    # Reading (one primary-key lookup; never touches sample rows)
    # ------------------------------------------------------------------
    def read(self, voyage: str | None = None, station: str | None = None,
             day: str | None = None) -> list[Summary]:
        clauses: list[str] = []
        params: list[Any] = []
        if voyage:
            clauses.append("voyage = ?")
            params.append(voyage)
        if station:
            clauses.append("station = ?")
            params.append(normalize_station(station))
        if day:
            clauses.append("sample_day = ?")
            params.append(sample_day(day))
        query = "SELECT * FROM daily_reports"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY voyage, station, sample_day"
        return [_public(row) for row in self.db.fetchall(query, params)]

    def group(self, voyage: str, station: str, day: str) -> Summary | None:
        row = self.db.fetchone(
            "SELECT * FROM daily_reports WHERE voyage = ? AND station = ? AND sample_day = ?",
            group_key(voyage, station, day),
        )
        return _public(row) if row else None

    # ------------------------------------------------------------------
    # Bottle register
    # ------------------------------------------------------------------
    def register_batch(self, batch_id: str, voyage: str, default_station: str | None,
                       bottles: Iterable[dict[str, Any]]) -> None:
        """Reconcile the registered bottles of one batch.

        ``bottles`` is the batch's current handover register; each item needs
        ``id``, ``station`` and ``sampled_at`` (``sample_start`` is accepted as
        a fallback sampling time).  Bottles that disappeared are removed, and
        a bottle that moved to another station/day carries its published value
        with it.

        The reconciliation compares desired state with stored state, so calling
        it twice with the same register changes nothing.
        """
        desired: dict[str, tuple[str, str, str]] = {}
        for bottle in bottles:
            station = bottle.get("station") or default_station
            moment = bottle.get("sampled_at") or bottle.get("sample_start")
            desired[bottle["id"]] = group_key(voyage, station, moment)

        signature = _register_signature(desired)
        stored_signature = self.db.scalar(
            "SELECT register_signature FROM daily_batches WHERE batch_id = ?", (batch_id,)
        )
        if stored_signature == signature:
            return
        stamp = now_iso()
        self.db.execute(
            "INSERT INTO daily_batches (batch_id, register_signature, updated_at) "
            "VALUES (?, ?, ?) ON CONFLICT(batch_id) DO UPDATE SET "
            "register_signature = excluded.register_signature, "
            "updated_at = excluded.updated_at",
            (batch_id, signature, stamp),
        )

        existing = {
            row["bottle_id"]: row for row in self.db.fetchall(
                "SELECT * FROM daily_bottle_facts WHERE batch_id = ?", (batch_id,)
            )
        }
        changes: dict[tuple[str, str, str], _GroupChange] = defaultdict(_GroupChange)
        inserts: list[list[Any]] = []
        moves: list[list[Any]] = []
        for bottle_id, group in desired.items():
            prior = existing.pop(bottle_id, None)
            if prior is None:
                inserts.append([bottle_id, batch_id, *group, 0, None, stamp])
                changes[group].bottles += 1
                continue
            old_group = (prior["voyage"], prior["station"], prior["sample_day"])
            if old_group == group:
                continue
            published = int(prior["published"] or 0)
            salinity = prior["salinity"]
            self._remove(changes[old_group], published, salinity)
            changes[group].bottles += 1
            changes[group].published += published
            if published and salinity is not None:
                changes[group].salinities[float(salinity)] += 1
            moves.append([group[0], group[1], group[2], stamp, bottle_id])

        for prior in existing.values():
            self._remove(changes[(prior["voyage"], prior["station"], prior["sample_day"])],
                         int(prior["published"] or 0), prior["salinity"])

        if inserts:
            self.db.executemany(
                "INSERT INTO daily_bottle_facts "
                "(bottle_id, batch_id, voyage, station, sample_day, published, salinity, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                inserts,
            )
        if moves:
            self.db.executemany(
                "UPDATE daily_bottle_facts SET voyage = ?, station = ?, sample_day = ?, "
                "updated_at = ? WHERE bottle_id = ?",
                moves,
            )
        if existing:
            self.db.executemany(
                "DELETE FROM daily_bottle_facts WHERE bottle_id = ?",
                [[bottle_id] for bottle_id in existing],
            )
        self._apply(changes)

    def move_bottle(self, bottle_id: str, voyage: str, station: str | None,
                    sampled_at: str | None) -> None:
        """Move one registered bottle to a new station/day."""
        fact = self.db.fetchone(
            "SELECT * FROM daily_bottle_facts WHERE bottle_id = ?", (bottle_id,)
        )
        if fact is None:
            return
        target = group_key(voyage, station, sampled_at)
        if (fact["voyage"], fact["station"], fact["sample_day"]) == target:
            return
        old_group = (fact["voyage"], fact["station"], fact["sample_day"])
        changes: dict[tuple[str, str, str], _GroupChange] = defaultdict(_GroupChange)
        self._remove(changes[old_group], int(fact["published"] or 0), fact["salinity"])
        changes[target].bottles += 1
        changes[target].published += int(fact["published"] or 0)
        if fact["published"] and fact["salinity"] is not None:
            changes[target].salinities[float(fact["salinity"])] += 1
        self.db.execute(
            "UPDATE daily_bottle_facts SET voyage = ?, station = ?, sample_day = ?, "
            "updated_at = ? WHERE bottle_id = ?",
            (target[0], target[1], target[2], now_iso(), bottle_id),
        )
        self._apply(changes)

    def remove_batch(self, batch_id: str) -> None:
        """Drop every fact of a batch (its handover scope disappeared)."""
        rows = self.db.fetchall(
            "SELECT * FROM daily_bottle_facts WHERE batch_id = ?", (batch_id,)
        )
        changes: dict[tuple[str, str, str], _GroupChange] = defaultdict(_GroupChange)
        for row in rows:
            self._remove(changes[(row["voyage"], row["station"], row["sample_day"])],
                         int(row["published"] or 0), row["salinity"])
        self.db.execute("DELETE FROM daily_bottle_facts WHERE batch_id = ?", (batch_id,))
        self.db.execute("DELETE FROM daily_batches WHERE batch_id = ?", (batch_id,))
        self._apply(changes)

    # ------------------------------------------------------------------
    # Publication
    # ------------------------------------------------------------------
    def set_bottles(self, batch_id: str, published: dict[str, float | None]) -> None:
        """Make the published state of this batch's bottles match ``published``.

        ``published`` maps bottle id -> salinity (``None`` when the result has
        no measured value); a bottle absent from it is unpublished.  The stored
        facts provide the previous state, so replaying the same mapping writes
        nothing and moves no counter -- a retried run cannot double count.

        Only this batch's fact rows are read; changes are aggregated per group
        so that a first publication of a large batch costs one maintenance pass
        per group rather than one per bottle.
        """
        rows = self.db.fetchall(
            "SELECT * FROM daily_bottle_facts WHERE batch_id = ?", (batch_id,)
        )
        if not rows:
            # No register yet; a later register pass publishes these bottles.
            return
        changes: dict[tuple[str, str, str], _GroupChange] = defaultdict(_GroupChange)
        stamp = now_iso()
        updates: list[list[Any]] = []
        for fact in rows:
            bottle_id = fact["bottle_id"]
            wanted = published.get(bottle_id)
            wanted = None if wanted is None else float(wanted)
            stored = float(fact["salinity"]) if fact["salinity"] is not None else None
            was = bool(fact["published"])
            now = bottle_id in published
            if was == now and stored == wanted:
                continue
            group = (fact["voyage"], fact["station"], fact["sample_day"])
            change = changes[group]
            if was and not now:
                change.published -= 1
                if stored is not None:
                    change.salinities[stored] -= 1
            elif now and not was:
                change.published += 1
                if wanted is not None:
                    change.salinities[wanted] += 1
            else:
                if stored is not None:
                    change.salinities[stored] -= 1
                if wanted is not None:
                    change.salinities[wanted] += 1
            updates.append([1 if now else 0, wanted, stamp, bottle_id])
        if updates:
            self.db.executemany(
                "UPDATE daily_bottle_facts SET published = ?, salinity = ?, updated_at = ? "
                "WHERE bottle_id = ?",
                updates,
            )
        self._apply(changes)

    def set_bottle(self, bottle_id: str, published: bool, salinity: float | None) -> None:
        """Set the published state of one registered bottle.

        This is a constant-size operation: one primary-key lookup on the fact
        row, one on the group summary, and at most one on a salinity bucket
        plus one neighbouring-index lookup.  It is what makes a single-bottle
        review independent of the size of the group.
        """
        fact = self.db.fetchone(
            "SELECT * FROM daily_bottle_facts WHERE bottle_id = ?", (bottle_id,)
        )
        if fact is None:
            # A bottle outside the register cannot contribute to a day defined
            # by the register; a later register pass picks it up if it appears.
            return
        wanted = float(salinity) if published and salinity is not None else None
        stored = float(fact["salinity"]) if fact["salinity"] is not None else None
        was = bool(fact["published"])
        if was == bool(published) and stored == wanted:
            return
        group = (fact["voyage"], fact["station"], fact["sample_day"])
        change = _GroupChange()
        if was and not published:
            change.published -= 1
            if stored is not None:
                change.salinities[stored] -= 1
        elif published and not was:
            change.published += 1
            if wanted is not None:
                change.salinities[wanted] += 1
        else:
            if stored is not None:
                change.salinities[stored] -= 1
            if wanted is not None:
                change.salinities[wanted] += 1
        self.db.execute(
            "UPDATE daily_bottle_facts SET published = ?, salinity = ?, updated_at = ? "
            "WHERE bottle_id = ?",
            (1 if published else 0, wanted, now_iso(), bottle_id),
        )
        self._apply({group: change})

    # ------------------------------------------------------------------
    # Applying differences
    # ------------------------------------------------------------------
    @staticmethod
    def _remove(change: _GroupChange, published: int, salinity: Any) -> None:
        change.bottles -= 1
        change.published -= published
        if published and salinity is not None:
            change.salinities[float(salinity)] -= 1

    def _apply(self, changes: dict[tuple[str, str, str], _GroupChange]) -> None:
        for group, change in changes.items():
            if not change.empty():
                self._apply_group(group, change)

    def _apply_group(self, group: tuple[str, str, str], change: _GroupChange) -> None:
        """Apply one group's differences to its summary and median cursor."""
        summary = self.db.fetchone(
            "SELECT * FROM daily_reports WHERE voyage = ? AND station = ? AND sample_day = ?",
            group,
        )
        stamp = now_iso()
        if summary is None:
            zero = {
                "voyage": group[0], "station": group[1], "sample_day": group[2],
                "bottle_count": 0, "published_count": 0, "salinity_count": 0,
                "lower_value": None, "lower_before": 0,
                "upper_value": None, "upper_before": 0,
                "median_value": None, "updated_at": stamp,
            }
            self.db.insert("daily_reports", zero)
            summary = zero

        salinities = {value: delta for value, delta in change.salinities.items() if delta}
        old_n = int(summary["salinity_count"] or 0)
        new_n = old_n + sum(salinities.values())
        bulk = len(salinities) > CURSOR_WALK_LIMIT
        if salinities:
            self._apply_buckets(group, salinities, bulk=bulk)

        lower_value = summary["lower_value"]
        lower_before = int(summary["lower_before"] or 0)
        upper_value = summary["upper_value"]
        upper_before = int(summary["upper_before"] or 0)
        if new_n <= 0:
            cursor = {
                "lower_value": None, "lower_before": 0,
                "upper_value": None, "upper_before": 0, "median_value": None,
            }
        elif bulk or lower_value is None or upper_value is None:
            # A first publication (no cursor yet) or a bulk change is settled
            # with one ordered aggregate rather than many cursor steps.
            lower_value, lower_before, upper_value, upper_before, median_value = \
                self._bulk_cursors(group, new_n)
            cursor = {
                "lower_value": lower_value, "lower_before": lower_before,
                "upper_value": upper_value, "upper_before": upper_before,
                "median_value": median_value,
            }
        else:
            # A cursor ranked above a changed value keeps its bucket; the
            # number of values below it moves with the change.
            for value, delta in salinities.items():
                if value < lower_value:
                    lower_before += delta
                if value < upper_value:
                    upper_before += delta
            lower_value, lower_before = self._locate(
                group, (new_n - 1) // 2, lower_value, lower_before)
            upper_value, upper_before = self._locate(
                group, new_n // 2, upper_value, upper_before)
            cursor = {
                "lower_value": lower_value, "lower_before": lower_before,
                "upper_value": upper_value, "upper_before": upper_before,
                "median_value": _median_of(lower_value, upper_value, new_n % 2 == 1),
            }

        bottles = int(summary["bottle_count"] or 0) + change.bottles
        published = int(summary["published_count"] or 0) + change.published
        if bottles <= 0 and published <= 0 and new_n <= 0:
            # The group has no registered bottles left (its handover scope
            # disappeared), so the summary itself should not linger.
            self.db.execute(
                "DELETE FROM daily_reports WHERE voyage = ? AND station = ? "
                "AND sample_day = ?",
                group,
            )
            return
        self.db.execute(
            "UPDATE daily_reports SET bottle_count = ?, published_count = ?, "
            "salinity_count = ?, lower_value = ?, lower_before = ?, upper_value = ?, "
            "upper_before = ?, median_value = ?, updated_at = ? "
            "WHERE voyage = ? AND station = ? AND sample_day = ?",
            (bottles, published, new_n,
             cursor["lower_value"], cursor["lower_before"],
             cursor["upper_value"], cursor["upper_before"], cursor["median_value"],
             stamp, *group),
        )

    def _apply_buckets(self, group: tuple[str, str, str], deltas: dict[float, int],
                       bulk: bool) -> None:
        if bulk:
            self.db.executemany(
                "INSERT INTO daily_salinity_buckets "
                "(voyage, station, sample_day, salinity_key, salinity, bottle_count) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(voyage, station, sample_day, salinity_key) DO UPDATE SET "
                "bottle_count = daily_salinity_buckets.bottle_count + excluded.bottle_count",
                [[*group, _salinity_key(value), value, delta]
                 for value, delta in deltas.items()],
            )
            self.db.execute(
                "DELETE FROM daily_salinity_buckets WHERE voyage = ? AND station = ? "
                "AND sample_day = ? AND bottle_count <= 0",
                group,
            )
            return
        for value, delta in deltas.items():
            key = _salinity_key(value)
            row = self.db.fetchone(
                "SELECT bottle_count FROM daily_salinity_buckets WHERE voyage = ? "
                "AND station = ? AND sample_day = ? AND salinity_key = ?",
                (*group, key),
            )
            count = (int(row["bottle_count"]) if row else 0) + delta
            if count <= 0:
                if row:
                    self.db.execute(
                        "DELETE FROM daily_salinity_buckets WHERE voyage = ? AND station = ? "
                        "AND sample_day = ? AND salinity_key = ?",
                        (*group, key),
                    )
            elif row:
                self.db.execute(
                    "UPDATE daily_salinity_buckets SET bottle_count = ? WHERE voyage = ? "
                    "AND station = ? AND sample_day = ? AND salinity_key = ?",
                    (count, *group, key),
                )
            else:
                self.db.insert("daily_salinity_buckets", {
                    "voyage": group[0], "station": group[1], "sample_day": group[2],
                    "salinity_key": key, "salinity": value, "bottle_count": count,
                })

    def _bucket(self, group: tuple[str, str, str], key: str) -> dict[str, Any] | None:
        return self.db.fetchone(
            "SELECT salinity, salinity_key, bottle_count FROM daily_salinity_buckets "
            "WHERE voyage = ? AND station = ? AND sample_day = ? AND salinity_key = ?",
            (*group, key),
        )

    def _neighbour(self, group: tuple[str, str, str], key: str, value: float | None,
                   below: bool) -> dict[str, Any] | None:
        """Return the adjacent non-empty bucket on the ordered salinity index."""
        if below:
            operator, order = "<", "DESC"
        else:
            operator, order = ">", "ASC"
        # Values are ordered numerically; the key breaks ties for buckets that
        # round to the same REAL, exactly matching the insertion ordering.
        return self.db.fetchone(
            "SELECT salinity, salinity_key, bottle_count FROM daily_salinity_buckets "
            "WHERE voyage = ? AND station = ? AND sample_day = ? "
            f"AND salinity {operator} ? "
            f"ORDER BY salinity {order}, salinity_key {order} LIMIT 1",
            (*group, value),
        )

    def _locate(self, group: tuple[str, str, str], target: int,
                value: float | None, before: int) -> tuple[float, int]:
        """Move the median cursor to ``target`` using neighbouring buckets.

        A one-bottle change moves the pointer by at most one bucket, so this
        loop runs a bounded number of index lookups and never reads the group's
        sample rows.
        """
        key = _salinity_key(value) if value is not None else None
        row = self._bucket(group, key) if key is not None else None
        if row is None:
            if key is not None and value is not None:
                pred = self._neighbour(group, key, value, below=True)
                if pred is not None:
                    row, key = pred, pred["salinity_key"]
                    before -= int(pred["bottle_count"])
                else:
                    succ = self._neighbour(group, key, value, below=False)
                    if succ is not None:
                        row, key, before = succ, succ["salinity_key"], 0
            if row is None:
                first = self.db.fetchone(
                    "SELECT salinity, salinity_key, bottle_count FROM daily_salinity_buckets "
                    "WHERE voyage = ? AND station = ? AND sample_day = ? "
                    "ORDER BY salinity ASC, salinity_key ASC LIMIT 1",
                    group,
                )
                if first is None:
                    return 0.0, 0
                row, key, before = first, first["salinity_key"], 0
        if before < 0:
            before = 0
        for _ in range(1_000_000):  # bounded: a single change moves one bucket
            if before > target:
                pred = self._neighbour(group, key, float(row["salinity"]), below=True)
                if pred is None:
                    before = max(0, target)
                    break
                before -= int(pred["bottle_count"])
                row, key = pred, pred["salinity_key"]
                continue
            if before + int(row["bottle_count"]) <= target:
                before += int(row["bottle_count"])
                succ = self._neighbour(group, key, float(row["salinity"]), below=False)
                if succ is None:
                    break
                row, key = succ, succ["salinity_key"]
                continue
            break
        if before < 0:
            before = 0
        return float(row["salinity"]), before

    def _bulk_cursors(self, group: tuple[str, str, str], total: int
                      ) -> tuple[float, int, float, int, float]:
        """Compute both middle values with one ordered window aggregate.

        Only used when the cursor is absent (a first publication) or a bulk
        change makes several buckets move at once; a single-bottle change uses
        :meth:`_locate` instead and never aggregates.
        """
        query = (
            "SELECT salinity, running, bottle_count FROM ("
            "  SELECT salinity, bottle_count, salinity_key, "
            "         SUM(bottle_count) OVER (ORDER BY salinity, salinity_key) AS running "
            "  FROM daily_salinity_buckets "
            "  WHERE voyage = ? AND station = ? AND sample_day = ?"
            ") AS ranked WHERE running > ? ORDER BY salinity, salinity_key LIMIT 1"
        )
        lower = self.db.fetchone(query, (*group, (total - 1) // 2))
        upper = self.db.fetchone(query, (*group, total // 2))
        if lower is None or upper is None:  # pragma: no cover - defensive
            return 0.0, 0, 0.0, 0, 0.0
        lower_value = float(lower["salinity"])
        lower_before = int(lower["running"]) - int(lower["bottle_count"])
        upper_value = float(upper["salinity"])
        upper_before = int(upper["running"]) - int(upper["bottle_count"])
        return (lower_value, lower_before, upper_value, upper_before,
                _median_of(lower_value, upper_value, total % 2 == 1))

    # ------------------------------------------------------------------
    # Repair and verification
    # ------------------------------------------------------------------
    def scan(self, voyage: str | None = None) -> dict[tuple[str, str, str], dict[str, Any]]:
        """Recompute the report from the sample tables, in memory.

        Deliberately simple and exact (it sorts the values), because it is only
        used by the offline verification command and by repair -- never on the
        request path.
        """
        query = (
            "SELECT b.id, b.voyage, b.station, b.status, "
            "bb.id AS bottle_id, bb.station AS bottle_station, bb.sampled_at, "
            "br.salinity AS salinity, br.state AS state "
            "FROM batches b JOIN batch_bottles bb ON bb.batch_id = b.id "
            "LEFT JOIN batch_results br ON br.batch_id = b.id "
            "AND br.bottle_id = bb.record_key AND br.batch_version = b.version "
        )
        params: tuple[Any, ...] = ()
        if voyage is not None:
            query += "WHERE b.voyage = ? "
            params = (voyage,)
        query += "ORDER BY b.id, bb.ordinal"
        groups: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in self.db.fetchall(query, params):
            group = group_key(row["voyage"],
                              row["bottle_station"] or row["station"],
                              row["sampled_at"])
            entry = groups.setdefault(group, {
                "bottle_count": 0, "published_count": 0, "values": [],
                "bottles": {},
            })
            entry["bottle_count"] += 1
            published = (row["status"] in ("published", "in_review")
                         and row["state"] in ("auto", "confirmed"))
            salinity = float(row["salinity"]) if row["salinity"] is not None else None
            entry["bottles"][row["bottle_id"]] = (published, salinity)
            if published:
                entry["published_count"] += 1
                if salinity is not None:
                    entry["values"].append(salinity)
        for entry in groups.values():
            values = sorted(entry["values"])
            count = len(values)
            if not count:
                entry["salinity_count"] = 0
                entry["salinity_median"] = None
            else:
                entry["salinity_count"] = count
                entry["salinity_median"] = _median_of(
                    values[(count - 1) // 2], values[count // 2], count % 2 == 1)
            del entry["values"]
        return groups

    def rebuild(self, voyage: str | None = None) -> None:
        """Recompute the whole report from the sample tables (offline repair).

        Normal operation never calls this; it exists so a verification run can
        prove that the incrementally maintained state agrees with the published
        samples, and so an operator can recover a hand-edited database.
        """
        groups = self.scan(voyage)
        clauses = "" if voyage is None else " WHERE voyage = ?"
        params: tuple[Any, ...] = () if voyage is None else (voyage,)
        self.db.execute("DELETE FROM daily_reports" + clauses, params)
        self.db.execute("DELETE FROM daily_bottle_facts" + clauses, params)
        self.db.execute("DELETE FROM daily_salinity_buckets" + clauses, params)
        if voyage is None:
            self.db.execute("DELETE FROM daily_batches")
        else:
            self.db.execute(
                "DELETE FROM daily_batches WHERE batch_id IN "
                "(SELECT id FROM batches WHERE voyage = ?)", params)

        stamp = now_iso()
        facts: list[list[Any]] = []
        registers: dict[str, dict[str, tuple[str, str, str]]] = {}
        for (voyage_code, station, day), entry in groups.items():
            for bottle_id, (published, salinity) in entry["bottles"].items():
                facts.append([bottle_id, None, voyage_code, station, day,
                              1 if published else 0, salinity, stamp])
        batch_of = self._bottle_batches(voyage)
        for fact in facts:
            fact[1] = batch_of.get(fact[0])
            if fact[1] is not None:
                registers.setdefault(fact[1], {})[fact[0]] = (fact[2], fact[3], fact[4])
        if facts:
            self.db.executemany(
                "INSERT INTO daily_bottle_facts "
                "(bottle_id, batch_id, voyage, station, sample_day, published, salinity, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                facts,
            )
        for batch_id, register in registers.items():
            self.db.insert("daily_batches", {
                "batch_id": batch_id,
                "register_signature": _register_signature(register),
                "updated_at": stamp,
            })
        for group, entry in groups.items():
            change = _GroupChange()
            change.bottles += entry["bottle_count"]
            change.published += entry["published_count"]
            for published, salinity in entry["bottles"].values():
                if published and salinity is not None:
                    change.salinities[salinity] += 1
            self._apply_group(group, change)

    def _bottle_batches(self, voyage: str | None) -> dict[str, str]:
        query = "SELECT bb.id AS bottle_id, bb.batch_id FROM batch_bottles bb "
        params: tuple[Any, ...] = ()
        if voyage is not None:
            query += "JOIN batches b ON b.id = bb.batch_id WHERE b.voyage = ?"
            params = (voyage,)
        return {row["bottle_id"]: row["batch_id"]
                for row in self.db.fetchall(query, params)}


def _register_signature(desired: dict[str, tuple[str, str, str]]) -> str:
    digest = hashlib.sha256()
    for bottle_id in sorted(desired):
        digest.update(bottle_id.encode("utf-8"))
        digest.update(b"\x00")
        digest.update("\x1f".join(desired[bottle_id]).encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


__all__ = ["DailyLedger", "bottle_id_for", "group_key", "sample_day", "UNKNOWN"]
