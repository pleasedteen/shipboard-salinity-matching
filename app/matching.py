from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .util import canonical_json, normalize_label, normalize_station, station_compare


@dataclass
class Bottle:
    key: str
    label_raw: str
    label_norm: str
    station: str | None
    sampled_at: str | None
    sample_end: str | None
    locator: dict[str, Any]
    confidence: float
    fingerprint: str
    deck_keys: list[str] = field(default_factory=list)
    deck_locators: list[dict[str, Any]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Result:
    key: str
    label_raw: str
    label_norm: str
    station: str | None
    sampled_at: str | None
    sample_end: str | None
    sampled_at_precision: str
    salinity: float | None
    batch_hint: str | None
    locator: dict[str, Any]
    confidence: float
    fingerprint: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Edge:
    bottle_key: str
    result_key: str
    score: float
    label_exact: bool
    station_exact: bool
    reasons: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {
            "bottle": self.bottle_key,
            "result": self.result_key,
            "score": self.score,
            "label_exact": self.label_exact,
            "station_exact": self.station_exact,
            "reasons": self.reasons,
        }


# ---------------------------------------------------------------------------
# Compatibility predicates
# ---------------------------------------------------------------------------

def label_match(bottle_label: str | None, result_label: str | None) -> tuple[bool, bool, float]:
    """Return (compatible, exact, quality).

    Laboratory transcripts sometimes copy only a fragment of the bottle label.
    Fragments shorter than three significant characters are not accepted
    because they would link unrelated bottles.
    """
    b = normalize_label(bottle_label)
    r = normalize_label(result_label)
    if not b or not r:
        return False, False, 0.0
    if b == r:
        return True, True, 1.0
    if len(r) >= 3 and r in b:
        return True, False, round(0.62 + min(0.18, 0.06 * (len(r) - 3)), 3)
    if len(b) >= 3 and b in r:
        return True, False, round(0.60 + min(0.15, 0.05 * (len(b) - 3)), 3)
    bnum = re.search(r"(\d{3,})$", b)
    rnum = re.search(r"(\d{3,})$", r)
    if bnum and rnum and len(r) <= 5 and bnum.group(1).endswith(rnum.group(1)):
        return True, False, 0.55
    return False, False, 0.0


def _parse_dt(value: str | None):
    from .util import parse_time

    if not value:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return parse_time(value + " 00:00")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", value):
        return parse_time(value.replace("T", " ") + ":00")
    return parse_time(value)


def time_match(
    result_time: str | None,
    precision: str,
    bottle_time: str | None,
    batch_start: str | None,
    batch_end: str | None,
) -> tuple[bool, float, str | None]:
    """Check a laboratory sampling time against the bottle/batch window."""
    from datetime import timedelta

    if not result_time:
        return True, 0.35, "result has no sampling time"
    t = _parse_dt(result_time)
    if t is None:
        return True, 0.30, "sampling time is not machine-readable"
    if precision == "day":
        if bottle_time:
            bt = _parse_dt(bottle_time)
            if bt is not None:
                if bt.date() != t.date():
                    return False, 0.0, "date differs from bottle record"
                return True, 0.62, "date-level match"
        if batch_start and batch_end:
            s, e = _parse_dt(batch_start), _parse_dt(batch_end)
            if s and e and (t.date() < s.date() or t.date() > e.date()):
                return False, 0.0, "date outside handover window"
        return True, 0.55, "date-level match"
    if bottle_time:
        bt = _parse_dt(bottle_time)
        if bt is not None:
            delta = abs((t - bt).total_seconds())
            if delta <= 15 * 60:
                return True, 1.0 if delta <= 5 * 60 else 0.92, None
            if delta <= 90 * 60:
                return True, 0.65, "time differs by more than 15 minutes"
            return False, 0.0, "time differs from bottle record"
    if batch_start:
        s = _parse_dt(batch_start)
        e = _parse_dt(batch_end) if batch_end else None
        if s is not None:
            if t < s - timedelta(minutes=2):
                return False, 0.0, "before handover window"
            if e is not None:
                if t > e + timedelta(minutes=2):
                    return False, 0.0, "after handover window"
                return True, 0.85, None
            if (t - s).total_seconds() > 12 * 3600:
                return False, 0.0, "far outside handover window"
            return True, 0.60, "handover window has no end time"
    return True, 0.50, "no window to compare"


def batch_hint_match(hint: str | None, batch_code: str) -> tuple[bool, float, str | None]:
    if not hint:
        return True, 0.5, None
    if normalize_station(hint) == normalize_station(batch_code):
        return True, 1.0, None
    return False, 0.0, "result names a different dispatch batch"


def edge_for(
    bottle: Bottle,
    result: Result,
    batch_code: str,
    batch_start: str | None,
    batch_end: str | None,
) -> Edge | None:
    """Build a feasible bottle-result edge, or ``None`` if incompatible."""
    if result.salinity is None:
        return None
    reasons: list[str] = []

    ok, exact, lquality = label_match(bottle.label_norm, result.label_norm)
    if not ok:
        return None
    if not exact:
        reasons.append("label fragment matched")

    if result.batch_hint:
        ok, _q, reason = batch_hint_match(result.batch_hint, batch_code)
        if not ok:
            return None

    station_exact = False
    if result.station and bottle.station:
        compatible, station_exact = station_compare(result.station, bottle.station)
        if not compatible:
            return None
        station_score = 1.0 if station_exact else 0.75
        if not station_exact:
            reasons.append("station prefix compatible")
    elif result.station and not bottle.station:
        station_score = 0.5
        reasons.append("bottle station absent")
    elif bottle.station and not result.station:
        station_score = 0.32
        reasons.append("result station inherited from report heading")
    else:
        station_score = 0.25

    ok, time_score, time_reason = time_match(
        result.sampled_at,
        result.sampled_at_precision,
        bottle.sampled_at,
        batch_start,
        batch_end,
    )
    if not ok:
        return None
    if time_reason:
        reasons.append(time_reason)

    hint_score = 1.0 if result.batch_hint else 0.5
    score = 0.34 * lquality + 0.26 * station_score + 0.25 * time_score + 0.15 * hint_score
    return Edge(
        bottle_key=bottle.key,
        result_key=result.key,
        score=round(min(0.999, score), 3),
        label_exact=exact,
        station_exact=station_exact,
        reasons=reasons,
    )


# ---------------------------------------------------------------------------
# Perfect matching and candidate sets
# ---------------------------------------------------------------------------

def _adjacency(bottles: Iterable[str], edges: Iterable[Edge]) -> dict[str, list[str]]:
    allowed = set(bottles)
    adjacency: dict[str, list[str]] = {b: [] for b in allowed}
    ordered = sorted(edges, key=lambda e: (-e.score, e.bottle_key, e.result_key))
    for edge in ordered:
        if edge.bottle_key in allowed and edge.result_key not in adjacency[edge.bottle_key]:
            adjacency[edge.bottle_key].append(edge.result_key)
    return adjacency


def find_matching(
    bottle_keys: list[str],
    edges: list[Edge],
    blocked_results: set[str] | None = None,
) -> dict[str, str] | None:
    """Maximum-cardinality matching; returns a full matching or ``None``."""
    blocked_results = blocked_results or set()
    adjacency = _adjacency(bottle_keys, (e for e in edges if e.result_key not in blocked_results))
    owner: dict[str, str] = {}

    def augment(bottle: str, seen: set[str]) -> bool:
        for result in adjacency.get(bottle, ()):
            if result in seen:
                continue
            seen.add(result)
            if result not in owner or augment(owner[result], seen):
                owner[result] = bottle
                return True
        return False

    order = sorted(bottle_keys, key=lambda b: (len(adjacency.get(b, ())), b))
    for bottle in order:
        if not augment(bottle, set()):
            return None
    if len(owner) != len(bottle_keys):
        return None
    return {bottle: result for result, bottle in owner.items()}


def _scc(nodes: list[str], graph: dict[str, set[str]]) -> dict[str, int]:
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    low: dict[str, int] = {}
    component: dict[str, int] = {}
    counter = 0

    def visit(node: str) -> None:
        nonlocal index, counter
        indices[node] = low[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for nxt in sorted(graph.get(node, ())):
            if nxt not in indices:
                visit(nxt)
                low[node] = min(low[node], low[nxt])
            elif nxt in on_stack:
                low[node] = min(low[node], indices[nxt])
        if low[node] == indices[node]:
            while True:
                item = stack.pop()
                on_stack.discard(item)
                component[item] = counter
                if item == node:
                    break
            counter += 1

    for node in nodes:
        if node not in indices:
            visit(node)
    return component


@dataclass
class Feasibility:
    perfect: bool
    assignment: dict[str, str]
    candidates: dict[str, list[str]]
    ambiguous: dict[str, list[str]]
    unmatched_bottles: list[str]
    matching_count_hint: int

    @property
    def unique(self) -> bool:
        return self.perfect and not self.ambiguous


def feasible(
    bottles: list[str],
    edges: list[Edge],
    pins: dict[str, str] | None = None,
    blocked_results: set[str] | None = None,
) -> Feasibility:
    """Find a perfect matching and every result that can belong to each bottle.

    If the materials cannot cover the whole batch, ``perfect`` is false and no
    pairings are published.  ``pins`` are reviewer-confirmed pairings; they are
    removed before the analysis so only completions consistent with them are
    considered.
    """
    pins = dict(pins or {})
    bottle_set = set(bottles)
    if not set(pins) <= bottle_set:
        return Feasibility(False, {}, {}, {}, sorted(bottle_set), 0)
    if len(set(pins.values())) != len(pins):
        return Feasibility(False, {}, {}, {}, sorted(bottle_set), 0)
    edge_map = {(e.bottle_key, e.result_key): e for e in edges}
    for bottle_key, result_key in pins.items():
        if (bottle_key, result_key) not in edge_map:
            return Feasibility(False, {}, {}, {}, sorted(bottle_set), 0)

    remaining = [b for b in bottles if b not in pins]
    blocked = set(pins.values()) | set(blocked_results or set())
    visible_edges = [e for e in edges if e.result_key not in blocked]
    matching = find_matching(remaining, visible_edges)
    if matching is None:
        return Feasibility(False, {}, {}, {}, sorted(remaining), 0)

    adjacency = _adjacency(remaining, visible_edges)
    owner = {result: bottle for bottle, result in matching.items()}

    # Alternating graph on bottles: u -> v when u is adjacent to the result
    # currently matched to v.  SCCs identify swaps on alternating cycles.
    graph: dict[str, set[str]] = {b: set() for b in remaining}
    free_capable: set[str] = set()
    for bottle in remaining:
        for result in adjacency.get(bottle, ()):
            counterpart = owner.get(result)
            if counterpart is None:
                # This bottle can move directly to an unmatched result.
                free_capable.add(bottle)
            elif counterpart != bottle:
                graph[bottle].add(counterpart)

    component = _scc(remaining, graph)
    reverse: dict[str, set[str]] = {b: set() for b in remaining}
    for bottle, counterparts in graph.items():
        for counterpart in counterparts:
            reverse.setdefault(counterpart, set()).add(bottle)

    # Nodes that can start an alternating path ending at an unmatched result.
    stack = list(free_capable)
    can_finish = set(free_capable)
    while stack:
        node = stack.pop()
        for predecessor in reverse.get(node, ()):  # pragma: no branch - small graph
            if predecessor not in can_finish:
                can_finish.add(predecessor)
                stack.append(predecessor)

    candidates: dict[str, list[str]] = {}
    ambiguous: dict[str, list[str]] = {}
    for bottle in remaining:
        options: set[str] = set()
        for result in adjacency.get(bottle, ()):
            counterpart = owner.get(result)
            if counterpart is None:
                allowed = True
            else:
                allowed = (
                    counterpart in can_finish
                    or component.get(counterpart) == component.get(bottle)
                )
            if allowed:
                options.add(result)
        if not options and bottle in matching:
            options.add(matching[bottle])
        ordered_options = sorted(options)
        candidates[bottle] = ordered_options
        if len(ordered_options) > 1:
            ambiguous[bottle] = ordered_options

    assignment = dict(pins)
    for bottle, options in candidates.items():
        if len(options) == 1:
            assignment[bottle] = options[0]
    # Defensive: never publish a result twice.
    seen: set[str] = set()
    if len(set(assignment.values())) != len(assignment):
        return Feasibility(False, {}, {}, {}, sorted(bottle_set), 0)
    return Feasibility(
        perfect=True,
        assignment=assignment,
        candidates=candidates,
        ambiguous=ambiguous,
        unmatched_bottles=[],
        matching_count_hint=len(ambiguous),
    )


def validate_full_assignment(
    assignment: dict[str, str],
    bottles: list[str],
    edges: list[Edge],
) -> tuple[bool, str | None]:
    bottle_set = set(bottles)
    if set(assignment) != bottle_set:
        missing = sorted(bottle_set - set(assignment))
        extra = sorted(set(assignment) - bottle_set)
        return False, f"assignment must cover every bottle (missing={missing}, extra={extra})"
    if len(set(assignment.values())) != len(assignment):
        return False, "each result can belong to only one bottle"
    edge_set = {(e.bottle_key, e.result_key) for e in edges}
    for bottle_key, result_key in assignment.items():
        if (bottle_key, result_key) not in edge_set:
            return False, f"pairing {bottle_key} -> {result_key} is not supported by the materials"
    return True, None


def complete_partial_assignment(
    partial: dict[str, str],
    bottles: list[str],
    edges: list[Edge],
) -> tuple[dict[str, str] | None, dict[str, list[str]], str | None]:
    """Expand reviewer pins into a full assignment when the support is unique."""
    result = feasible(bottles, edges, pins=partial)
    if not result.perfect:
        return None, {}, "the confirmed pairings do not leave a way to cover the whole batch"
    if result.ambiguous:
        return None, result.candidates, (
            "the confirmed pairings still leave more than one complete pairing; "
            "confirm the remaining bottles as well"
        )
    return result.assignment, result.candidates, None


def assignment_context(
    batch_code: str,
    bottles: list[Bottle],
    results: list[Result],
    edges: list[Edge],
    global_facts: dict[str, Any] | None = None,
) -> str:
    """Fingerprint the semantic facts and feasible options behind an assignment.

    Salinity values and prose are deliberately excluded: correcting a measured
    value must not invalidate a reviewer's pairing.  Any change to bottles,
    matching labels/stations/times, or the set of feasible pairings does.
    """
    payload = {
        "batch": normalize_station(batch_code),
        "bottles": sorted(
            [
                {
                    "key": b.key,
                    "label": b.label_norm,
                    "station": normalize_station(b.station),
                    "sampled_at": b.sampled_at,
                    "sample_end": b.sample_end,
                }
                for b in bottles
            ],
            key=lambda x: x["key"],
        ),
        "results": sorted(
            [
                {
                    "key": r.key,
                    "label": r.label_norm,
                    "station": normalize_station(r.station),
                    "sampled_at": r.sampled_at,
                    "batch_hint": normalize_station(r.batch_hint),
                }
                for r in results
            ],
            key=lambda x: x["key"],
        ),
        "edges": sorted([e.bottle_key, e.result_key] for e in edges),
    }
    if global_facts is not None:
        payload["global_allocation"] = global_facts
    return canonical_json(payload)
