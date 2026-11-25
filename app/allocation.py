"""Voyage-wide allocation of laboratory results to complete batches.

The unit optimized here is a whole dispatch batch: a batch counts only when
*every* bottle can receive a distinct supported result.  Results are shared
resources across sibling batches, so a greedy batch-by-batch claim can get
stuck even when an alternating reassignment would complete more batches.

This module is intentionally deterministic and dependency-free.  Contention
graphs found in the review workflow are small; independent contention groups
are solved separately instead of forming one exponential voyage-wide search.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import sys
from typing import Any, Iterable, Sequence

from .matching import Edge, feasible as matching_feasible, find_matching

sys.setrecursionlimit(max(10_000, sys.getrecursionlimit()))


@dataclass(frozen=True)
class AllocationGroup:
    key: str
    bottles: tuple[str, ...]
    edges: tuple[Edge, ...]
    pins: tuple[tuple[str, str], ...] = ()
    # Lower tuples are selected preferentially when several maximum-count
    # allocations exist.  This preserves stable API/review output.
    priority: tuple[Any, ...] = ()


@dataclass
class GroupView:
    key: str
    reachable: bool = False
    always_selected: bool = False
    priority_selected: bool = False
    candidates: dict[str, list[str]] = field(default_factory=dict)
    forced: dict[str, str] = field(default_factory=dict)
    chosen: dict[str, str] | None = None
    # Interface data used by the service to expand exclusive-result choices
    # without enumerating their Cartesian product in the voyage solver.
    shared_results: list[str] = field(default_factory=list)
    interfaces: list[dict[str, str]] = field(default_factory=list)
    optimal_indexes: list[int] = field(default_factory=list)
    chosen_index: int | None = None


@dataclass(frozen=True)
class InterfaceOption:
    mask: int
    shared_pairs: dict[str, str]
    match: dict[str, str]


@dataclass
class AllocationReport:
    max_batches: int
    views: dict[str, GroupView]
    components: list[set[str]]


class _Packer:
    """Exact set-packing DP for complete batch matchings in one component."""

    NEG = -10**9

    def __init__(self, option_masks: list[list[int]], required: set[int] | None = None):
        # Do not de-duplicate masks here: reconstruction option indexes are
        # used by callers to recover a representative assignment.  Callers that
        # only need the optimum count may de-duplicate before construction.
        self.options = [tuple(masks) for masks in option_masks]
        self.required = required or set()
        self.n = len(self.options)
        self.future_mask = [0] * (self.n + 1)
        for index in range(self.n - 1, -1, -1):
            mask = self.future_mask[index + 1]
            for option_mask in self.options[index]:
                mask |= option_mask
            self.future_mask[index] = mask
        self._cache: dict[tuple[int, int], int] = {}

    def maximum(self) -> int:
        value = self._dp(0, 0)
        return max(0, value)

    def feasible_maximum(self) -> int:
        return self._dp(0, 0)

    def _dp(self, index: int, used: int) -> int:
        if index >= self.n:
            return 0
        cache_key = (index, used)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        if index in self.required:
            best = self.NEG
        else:
            next_used = used & self.future_mask[index + 1]
            best = self._dp(index + 1, next_used)
        for mask in self.options[index]:
            if mask & used:
                continue
            next_used = (used | mask) & self.future_mask[index + 1]
            tail = self._dp(index + 1, next_used)
            if tail < 0:
                continue
            value = 1 + tail
            if value > best:
                best = value
        self._cache[cache_key] = best
        return best

    def reconstruct(self, prefer_group: bool = True) -> list[int | None]:
        """Return an option index for each group, or ``None`` if skipped."""
        chosen: list[int | None] = [None] * self.n
        used = 0
        for index in range(self.n):
            target = self._dp(index, used)
            for option_index, mask in enumerate(self.options[index]):
                if mask & used:
                    continue
                next_used = (used | mask) & self.future_mask[index + 1]
                tail = self._dp(index + 1, next_used)
                if tail < 0:
                    continue
                if 1 + tail == target:
                    chosen[index] = option_index
                    used = next_used
                    break
            if chosen[index] is None and index in self.required:
                # Validated pins should prevent this state.  Stop rather than
                # silently dropping a reviewer-confirmed batch.
                break
        return chosen


def enumerate_matchings(
    bottles: Sequence[str] | tuple[str, ...] | list[str],
    edges: Iterable[Edge],
    pins: dict[str, str] | tuple[tuple[str, str], ...] | None = None,
) -> list[dict[str, str]]:
    """Enumerate every perfect bottle-to-result matching for one group."""
    bottle_list = list(bottles)
    bottle_set = set(bottle_list)
    pin_map = dict(pins or {})
    if not set(pin_map) <= bottle_set:
        return []
    if len(set(pin_map.values())) != len(pin_map):
        return []

    adjacency: dict[str, list[str]] = {b: [] for b in bottle_list}
    edge_set: set[tuple[str, str]] = set()
    for edge in edges:
        if edge.bottle_key not in bottle_set:
            continue
        pair = (edge.bottle_key, edge.result_key)
        if pair in edge_set:
            continue
        edge_set.add(pair)
        adjacency[edge.bottle_key].append(edge.result_key)

    for bottle, result in pin_map.items():
        if (bottle, result) not in edge_set:
            return []

    pinned_results = set(pin_map.values())
    remaining = [b for b in bottle_list if b not in pin_map]
    for bottle in remaining:
        adjacency[bottle] = [r for r in adjacency[bottle] if r not in pinned_results]
        if not adjacency[bottle]:
            return []

    # Deterministic degree-first search keeps enumeration compact.
    order = sorted(remaining, key=lambda b: (len(adjacency[b]), b))
    results: list[dict[str, str]] = []
    assignment = dict(pin_map)
    used = set(pinned_results)

    def dfs(position: int) -> None:
        if position == len(order):
            results.append(dict(assignment))
            return
        bottle = order[position]
        for result in sorted(adjacency[bottle]):
            if result in used:
                continue
            used.add(result)
            assignment[bottle] = result
            dfs(position + 1)
            del assignment[bottle]
            used.remove(result)

    dfs(0)
    return results


def build_interface_options(
    group: AllocationGroup,
    shared_results: set[str],
    shared_index: dict[str, int],
) -> list[InterfaceOption]:
    """Enumerate only a batch's use of voyage-contended results.

    Results exclusive to this batch are matched by the ordinary bipartite
    matcher during each feasibility probe.  This keeps 24 independent local
    ambiguities from becoming one exponential Cartesian enumeration.
    """
    bottle_list = list(group.bottles)
    bottle_set = set(bottle_list)
    pin_map = dict(group.pins)
    if not set(pin_map) <= bottle_set:
        return []
    if len(set(pin_map.values())) != len(pin_map):
        return []

    adjacency: dict[str, list[str]] = {b: [] for b in bottle_list}
    edge_set: set[tuple[str, str]] = set()
    for edge in group.edges:
        if edge.bottle_key not in bottle_set:
            continue
        pair = (edge.bottle_key, edge.result_key)
        if pair in edge_set:
            continue
        edge_set.add(pair)
        adjacency[edge.bottle_key].append(edge.result_key)

    for bottle, result in pin_map.items():
        if (bottle, result) not in edge_set:
            return []

    choice_bottles = [b for b in bottle_list if b not in pin_map]
    shared_adjacency = {
        b: sorted(r for r in adjacency[b] if r in shared_results)
        for b in choice_bottles
    }
    blocked_base = set(shared_results) | set(pin_map.values())
    options: list[InterfaceOption] = []
    shared_pairs: dict[str, str] = {
        b: r for b, r in pin_map.items() if r in shared_results
    }
    used_bottles = set(pin_map)
    used_results = set(pin_map.values())

    def record_if_complete() -> None:
        remaining = [b for b in bottle_list if b not in used_bottles]
        completion = find_matching(
            remaining, list(group.edges), blocked_results=set(blocked_base)
        )
        if completion is None:
            return
        full_match = dict(pin_map)
        full_match.update(shared_pairs)
        full_match.update(completion)
        mask = 0
        for result in full_match.values():
            bit = shared_index.get(result)
            if bit is not None:
                mask |= 1 << bit
        options.append(InterfaceOption(
            mask=mask,
            shared_pairs=dict(shared_pairs),
            match=full_match,
        ))

    def dfs(position: int) -> None:
        if position == len(choice_bottles):
            record_if_complete()
            return
        bottle = choice_bottles[position]
        # This bottle uses an exclusive result (or no contended result).
        dfs(position + 1)
        for result in shared_adjacency[bottle]:
            if result in used_results:
                continue
            shared_pairs[bottle] = result
            used_bottles.add(bottle)
            used_results.add(result)
            dfs(position + 1)
            used_results.remove(result)
            used_bottles.discard(bottle)
            del shared_pairs[bottle]

    dfs(0)
    return options


class _ComponentSolver:
    def __init__(self, groups: list[AllocationGroup]):
        # Service passes priority-sorted groups, but sort defensively.
        self.groups = sorted(groups, key=lambda g: (g.priority, g.key))
        owners: dict[str, set[str]] = {}
        for group in self.groups:
            for edge in group.edges:
                owners.setdefault(edge.result_key, set()).add(group.key)
        self.shared_results = {
            result for result, group_keys in owners.items() if len(group_keys) > 1
        }
        self.shared_index: dict[str, int] = {
            result: index for index, result in enumerate(sorted(self.shared_results))
        }

        self.interface_options: list[list[InterfaceOption]] = []
        self.matchings: list[list[dict[str, str]]] = []
        self.masks: list[list[int]] = []
        for group in self.groups:
            options = build_interface_options(
                group, self.shared_results, self.shared_index
            )
            self.interface_options.append(options)
            self.matchings.append([option.match for option in options])
            self.masks.append([option.mask for option in options])

        self.required = {
            index for index, group in enumerate(self.groups) if group.pins
        }

    def maximum(self, masks: list[list[int]] | None = None) -> int:
        return _Packer(
            self.masks if masks is None else masks,
            required=self.required,
        ).maximum()

    def solve_views(self) -> tuple[int, dict[str, GroupView]]:
        k = self.maximum()
        packer = _Packer(self.masks, required=self.required)
        option_choices = packer.reconstruct()

        views = {g.key: GroupView(key=g.key) for g in self.groups}
        for index, group in enumerate(self.groups):
            view = views[group.key]
            choice = option_choices[index]
            view.priority_selected = choice is not None
            if choice is not None and choice < len(self.matchings[index]):
                view.chosen = dict(self.matchings[index][choice])

            # Is this group present in every maximum solution?  A pinned group
            # is mandatory in the current review state by definition.
            if group.pins:
                view.always_selected = True
            else:
                without_group = [list(m) for m in self.masks]
                without_group[index] = []
                view.always_selected = self.maximum(without_group) < k

            view.shared_results = sorted(self.shared_results)
            view.interfaces = [
                dict(option.shared_pairs) for option in self.interface_options[index]
            ]
            view.chosen_index = choice

            # Identify interface usages that can be embedded in a K-batch
            # optimum.  The service expands exclusive-result alternatives for
            # each optimal interface.
            optimal_indexes: list[int] = []
            for option_index, option in enumerate(self.interface_options[index]):
                required_masks = [list(m) for m in self.masks]
                required_masks[index] = [option.mask]
                if self.maximum(required_masks) == k:
                    optimal_indexes.append(option_index)
            view.optimal_indexes = optimal_indexes

            candidate_sets: dict[str, set[str]] = {}
            for option_index in optimal_indexes:
                for bottle, result in self.matchings[index][option_index].items():
                    candidate_sets.setdefault(bottle, set()).add(result)
            view.candidates = {
                bottle: sorted(results) for bottle, results in sorted(candidate_sets.items())
            }
            view.reachable = bool(optimal_indexes) and bool(group.bottles)

            # Pins are constraints of the current review state.  Expand the
            # interface choices through the ordinary local matcher so exclusive
            # alternatives are included without Cartesian enumeration.
            for bottle, result in dict(group.pins).items():
                view.forced[bottle] = result
            self._expand_candidates(group, view)
        return k, views

    def _expand_candidates(self, group: AllocationGroup, view: GroupView) -> None:
        bottle_keys = list(group.bottles)
        pins = dict(group.pins)
        union: dict[str, set[str]] = {key: set() for key in bottle_keys}
        fixed_value: dict[str, str] = {}
        always_singleton = {key: True for key in bottle_keys}
        valid = 0

        for option_index in view.optimal_indexes:
            if option_index >= len(view.interfaces):
                continue
            shared_pairs = view.interfaces[option_index]
            conditional_pins = dict(pins)
            conditional_pins.update(shared_pairs)
            blocked_shared = set(view.shared_results) - set(shared_pairs.values())
            conditional = matching_feasible(
                bottle_keys,
                list(group.edges),
                pins=conditional_pins,
                blocked_results=blocked_shared,
            )
            if not conditional.perfect:
                continue
            valid += 1
            for bottle_key in bottle_keys:
                if bottle_key in conditional_pins:
                    options = {conditional_pins[bottle_key]}
                else:
                    options = set(conditional.candidates.get(bottle_key, ()))
                union[bottle_key] |= options
                if len(options) == 1:
                    result = next(iter(options))
                    previous = fixed_value.get(bottle_key)
                    if previous is not None and previous != result:
                        always_singleton[bottle_key] = False
                    else:
                        fixed_value[bottle_key] = result
                else:
                    always_singleton[bottle_key] = False

        if valid == 0:
            view.reachable = False
            return
        view.candidates = {
            bottle_key: sorted(options)
            for bottle_key, options in union.items()
        }
        for bottle_key in bottle_keys:
            if always_singleton.get(bottle_key) and bottle_key in fixed_value:
                view.forced[bottle_key] = fixed_value[bottle_key]
        view.reachable = all(view.candidates.get(key) for key in bottle_keys)


def _component_lists(groups: list[AllocationGroup]) -> list[list[AllocationGroup]]:
    by_key = {g.key: g for g in groups}
    parent = {g.key: g.key for g in groups}

    def find(key: str) -> str:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    result_owners: dict[str, list[str]] = {}
    for group in groups:
        for edge in group.edges:
            result_owners.setdefault(edge.result_key, []).append(group.key)
    for owners in result_owners.values():
        for owner in owners[1:]:
            union(owners[0], owner)

    component_map: dict[str, list[AllocationGroup]] = {}
    for group in groups:
        key = find(group.key)
        component_map.setdefault(key, []).append(by_key[group.key])
    return list(component_map.values())


def contention_components(groups: list[AllocationGroup]) -> list[set[str]]:
    """Return independent batch contention components."""
    return [{group.key for group in component} for component in _component_lists(groups)]


def maximum_complete_batches(groups: list[AllocationGroup]) -> int:
    """Return only the optimum count, avoiding expensive view derivation."""
    total = 0
    for component in _component_lists(groups):
        total += _ComponentSolver(component).maximum()
    return total


def solve_allocations(groups: list[AllocationGroup]) -> AllocationReport:
    """Solve all independent contention components in a voyage."""
    report_views: dict[str, GroupView] = {}
    components: list[set[str]] = []
    total = 0
    for component_groups in _component_lists(groups):
        components.append({group.key for group in component_groups})
        solver = _ComponentSolver(component_groups)
        k, views = solver.solve_views()
        total += k
        report_views.update(views)

    # Groups with no edges still receive an empty view for predictable access.
    for group in groups:
        report_views.setdefault(group.key, GroupView(key=group.key))
    return AllocationReport(max_batches=total, views=report_views, components=components)


def maximum_with_pins(
    groups: list[AllocationGroup],
    extra_pins: dict[str, dict[str, str]] | None = None,
) -> int:
    """Return the maximum complete-batch count after applying extra pins."""
    altered: list[AllocationGroup] = []
    extra_pins = extra_pins or {}
    for group in groups:
        pins = dict(group.pins)
        pins.update(extra_pins.get(group.key, {}))
        altered.append(AllocationGroup(
            key=group.key,
            bottles=group.bottles,
            edges=group.edges,
            pins=tuple(sorted(pins.items())),
            priority=group.priority,
        ))
    return maximum_complete_batches(altered)
