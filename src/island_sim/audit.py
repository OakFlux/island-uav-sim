"""Reconstruct task delivery, overdue exposure, energy and resource use from events."""

from __future__ import annotations

import math
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Mapping

from .domain import AircraftProfile, PeriodicStream, Scenario

TIME_TOL = 1e-6
ENERGY_TOL = 1e-6
ENERGY_FIELDS = ("consumed_wh", "charged_wh", "grid_wh", "external_wh")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _close(a: float, b: float, tolerance: float = TIME_TOL) -> bool:
    return math.isclose(a, b, rel_tol=0.0, abs_tol=tolerance)


def _number(event: Mapping, field: str) -> float:
    value = event[field]
    _require(type(value) in (int, float) and math.isfinite(value), f"Invalid number for {field}: {value!r}")
    return float(value)


def _text(event: Mapping, field: str) -> str:
    value = event[field]
    _require(isinstance(value, str) and bool(value), f"Invalid identifier for {field}: {value!r}")
    return value


@dataclass
class _Task:
    asset_id: str
    template_id: str
    stream_id: str | None
    released_s: float
    deadline_s: float
    owner: str | None = None
    units_completed: int = 0
    interruptions: int = 0
    carried_s: float = 0.0
    last_unit_s: float | None = None
    completed_s: float | None = None


@dataclass
class _Attempt:
    task_id: str
    aircraft_id: str
    order: int
    started_s: float
    carried_s: float


@dataclass
class _Energy:
    initial_wh: float
    current_wh: float
    consumed_wh: float = 0.0
    charged_wh: float = 0.0
    grid_wh: float = 0.0
    external_wh: float = 0.0
    last_interval_end_s: float = 0.0


class _Audit:
    def __init__(self, scenario: Scenario, mode: str, periodic_release_fraction: float,
                 max_targets_per_sortie: int | None = None) -> None:
        self.scenario = scenario
        self.mode = mode
        self.periodic_release_fraction = periodic_release_fraction
        self.expected_sortie_limit = max_targets_per_sortie
        self.sortie_limit: int | None = None
        self.errors: list[str] = []
        self.batch = {task.task_id: task for task in scenario.batch_tasks}
        self.streams = {stream.task_stream_id: stream for stream in scenario.periodic_streams}
        self.profiles = {
            member.aircraft_id: scenario.profiles[member.profile_id] for member in scenario.fleet
        }
        self.energy = {}
        for member in scenario.fleet:
            initial = self.profiles[member.aircraft_id].battery_usable_wh * member.initial_soc_fraction
            self.energy[member.aircraft_id] = _Energy(initial, initial)
        self.window_bounds = {
            "evaluation": (scenario.evaluation_start_s, scenario.evaluation_end_s),
            "recovery": (scenario.evaluation_end_s, scenario.recovery_end_s),
        }
        self.window_energy = {
            window: {aircraft: dict.fromkeys(ENERGY_FIELDS, 0.0) for aircraft in self.energy}
            for window in self.window_bounds
        }
        self.battery_snapshots = {
            "evaluation_start": dict.fromkeys(self.energy),
            "evaluation_end": dict.fromkeys(self.energy),
        }
        self.active_sorties: dict[str, dict] = {}
        self.sorties: list[dict] = []
        self.pending_visits: dict[str, tuple[str, float]] = {}
        self.air_allocations: dict[str, int] = dict.fromkeys(self.energy, 0)
        self.evaluation_air_allocations: dict[str, int] = dict.fromkeys(self.energy, 0)
        self.all_failures: Counter = Counter()
        self.movement_seconds = 0.0
        self.evaluation_movement_seconds = 0.0
        self.recovery_movement_seconds = 0.0
        self.tasks: dict[str, _Task] = {}
        self.active: dict[str, _Attempt] = {}
        self.active_aircraft: dict[str, str] = {}
        self.assigned_aircraft: dict[str, str] = {}
        self.pending_streams: dict[str, str] = {}
        self.completed_by_stream: dict[str, list[float]] = {
            stream_id: [] for stream_id in self.streams
        }
        self.weather_starts = tuple(interval.start_s for interval in scenario.weather)
        self.weather_by_start = {interval.start_s: interval for interval in scenario.weather}
        self.world_weather_starts: set[float] = set()
        self.last_observed_start = -math.inf
        self.capacities = {
            "air": scenario.station.air_capacity,
            "pad": scenario.station.pad_capacity,
            "charge": scenario.station.charge_ports,
        }
        self.held: dict[str, dict[str, float]] = {kind: {} for kind in self.capacities}
        self.resource_peak = dict.fromkeys(self.capacities, 0)
        self.resource_seconds = dict.fromkeys(self.capacities, 0.0)
        self.queued: dict[str, dict[str, float]] = {"landing": {}, "charge": {}}
        self.queue_wait_seconds = dict.fromkeys(self.queued, 0.0)
        self.evaluation_queue_wait_seconds = dict.fromkeys(self.queued, 0.0)
        self.evaluation_resource_seconds = dict.fromkeys(self.capacities, 0.0)
        self.recovery_resource_seconds = dict.fromkeys(self.capacities, 0.0)
        self.diagnostics: dict[str, Counter] = {
            "decision_reasons": Counter(), "candidate_rejections": Counter(),
            "recoveries": Counter(), "aircraft_failures": Counter(),
            "interruptions": Counter(), "dispatch_blocks": Counter(),
            "assignments_by_template": Counter(), "arrivals_by_template": Counter(),
            "units_completed_by_template": Counter(),
        }
        self.terminal: dict[str, Mapping] = {}
        self.markers: dict[str, float] = {}
        self.last_time = 0.0
        self.last_sequence: int | None = None
        self.event_count = 0

    def read(self, event: Mapping) -> None:
        try:
            time = _number(event, "time_s")
            kind = _text(event, "kind")
            sequence = event["sequence"]
            _require(type(sequence) is int and sequence >= 0, "Invalid event sequence")
            if self.last_sequence is None:
                _require(sequence == 0 and kind == "run_started", "Ledger does not begin at run_started")
            else:
                _require(sequence == self.last_sequence + 1, "Missing or disordered event sequence")
                _require("run_finished" not in self.markers, "Event after run_finished")
            _require(self.last_time - TIME_TOL <= time <= self.scenario.recovery_end_s + TIME_TOL,
                     "Event time decreased or exceeded the recovery horizon")
            self.last_time = time
            self.last_sequence = sequence
            self.event_count += 1
            self._event(kind, time, event)
            if self.scenario.evaluation_start_s <= time <= self.scenario.evaluation_end_s:
                self._diagnostic(kind, event)
        except (KeyError, TypeError, ValueError) as exc:
            sequence = event.get("sequence", "?") if isinstance(event, Mapping) else "?"
            self.errors.append(f"event {sequence}: {exc}")

    def _event(self, kind: str, time: float, event: Mapping) -> None:
        if kind in {"run_started", "evaluation_closed", "run_finished"}:
            _require(kind not in self.markers, f"Duplicate run marker: {kind}")
            if kind == "run_started":
                _require(event.get("scenario_id") == self.scenario.scenario_id
                         and event.get("execution_mode") == self.mode,
                         "Ledger run identity does not match audit settings")
                if "periodic_release_fraction" in event:
                    _require(_close(_number(event, "periodic_release_fraction"), self.periodic_release_fraction),
                             "Ledger periodic release setting does not match the audit")
                limit = event.get("max_targets_per_sortie")
                _require(limit is None or type(limit) is int and limit in {1, 2},
                         "Invalid sortie target limit in run marker")
                if self.expected_sortie_limit is not None:
                    _require(limit == self.expected_sortie_limit,
                             "Ledger sortie target limit does not match audit settings")
                self.sortie_limit = limit
            self.markers[kind] = time
        elif kind == "task_released":
            self._released(time, event)
        elif kind in {"task_assigned", "task_interrupted"}:
            self._ownership(kind, time, event)
        elif kind in {"unit_started", "unit_completed", "unit_interrupted"}:
            self._unit_event(kind, time, event)
        elif kind == "task_completed":
            self._completed(time, event)
        elif kind == "energy_interval":
            self._energy_interval(time, event)
        elif kind in {"resource_acquired", "resource_released"}:
            self._resource(kind, time, event)
        elif kind in {"queue_entered", "queue_left"}:
            self._queue(kind, time, event)
        elif kind in {"weather_changed", "observation_weather"}:
            self._weather(kind, time, event)
        elif kind == "aircraft_terminal":
            self._aircraft_terminal(event)
        elif kind in {"sortie_started", "target_visited", "sortie_finished"}:
            self._sortie(kind, time, event)
        elif kind == "arrived":
            self._arrived(time, event)
        elif kind == "aircraft_failed":
            self.all_failures[_text(event, "reason")] += 1

    def _diagnostic(self, kind: str, event: Mapping) -> None:
        cause_tables = {
            "recovery_requested": "recoveries", "aircraft_failed": "aircraft_failures",
            "dispatch_blocked": "dispatch_blocks",
        }
        template_tables = {
            "task_assigned": "assignments_by_template", "arrived": "arrivals_by_template",
            "unit_completed": "units_completed_by_template",
        }
        if kind == "decision":
            self.diagnostics["decision_reasons"][event.get("reason") or "accepted"] += 1
            for cause, count in event.get("candidate_rejections", {}).items():
                _require(type(count) is int and count >= 0, f"Invalid rejection count: {cause}")
                self.diagnostics["candidate_rejections"][cause] += count
        elif kind == "unit_interrupted":
            template = self.tasks[_text(event, "task_id")].template_id
            self.diagnostics["interruptions"][(template, event.get("reason") or "unspecified")] += 1
        elif kind in cause_tables:
            self.diagnostics[cause_tables[kind]][event.get("reason") or "unspecified"] += 1
        elif kind in template_tables:
            self.diagnostics[template_tables[kind]][_text(event, "template_id")] += 1

    def _evaluation_overlap(self, start: float, end: float) -> float:
        return max(0.0, min(end, self.scenario.evaluation_end_s) -
                   max(start, self.scenario.evaluation_start_s))

    def _released(self, time: float, event: Mapping) -> None:
        key = _text(event, "task_id")
        asset = _text(event, "asset_id")
        template = _text(event, "template_id")
        stream_id = event.get("stream_id")
        deadline = _number(event, "deadline_s")
        _require(key not in self.tasks, f"Duplicate task release: {key}")
        _require(time <= self.scenario.evaluation_end_s + TIME_TOL, f"Release after evaluation: {key}")
        if self.scenario.business == "batch":
            task = self.batch[key]
            _require(stream_id is None and (asset, template) == (task.asset_id, task.template_id)
                     and _close(time, task.release_s) and _close(deadline, task.deadline_s),
                     f"Batch release does not match scenario: {key}")
        else:
            stream = self.streams[stream_id]
            _require(stream_id not in self.pending_streams, f"Overlapping instances for stream {stream_id}")
            completions = self.completed_by_stream[stream_id]
            last = completions[-1] if completions else -stream.initial_age_s
            expected = max(0.0, last + self.periodic_release_fraction * stream.target_interval_s)
            _require((asset, template) == (stream.asset_id, stream.template_id)
                     and _close(time, expected)
                     and _close(deadline, last + stream.target_interval_s),
                     f"Periodic release does not match recurrence: {key}")
            self.pending_streams[stream_id] = key
        self.tasks[key] = _Task(asset, template, stream_id, time, deadline)

    def _ownership(self, kind: str, time: float, event: Mapping) -> None:
        key = _text(event, "task_id")
        task = self.tasks[key]
        aircraft = _text(event, "aircraft_id")
        _require(aircraft in self.profiles and task.completed_s is None, f"Invalid task ownership: {key}")
        if kind == "task_assigned":
            _require(task.owner is None and aircraft not in self.assigned_aircraft
                     and time < self.scenario.evaluation_end_s,
                     f"Task or aircraft already assigned or dispatch closed: {key} {aircraft}")
            task.owner = aircraft
            self.assigned_aircraft[aircraft] = key
        else:
            _require(task.owner == aircraft and key not in self.active,
                     f"Wrong owner or still-active unit at task interruption: {key}")
            task.owner = None
            del self.assigned_aircraft[aircraft]

    def _unit_event(self, kind: str, time: float, event: Mapping) -> None:
        key = _text(event, "task_id")
        task = self.tasks[key]
        aircraft = _text(event, "aircraft_id")
        profile = self.profiles[aircraft]
        order = event["unit_order"]
        progress = _number(event, "progress_s")
        units = self.scenario.templates[task.template_id]
        _require(type(order) is int and 1 <= order <= len(units)
                 and order == task.units_completed + 1, f"Invalid inspection order for {key}")
        unit = units[order - 1]
        _require(unit.required_capability in profile.capabilities,
                 f"Aircraft {aircraft} lacks the capability for {key} unit {order}")
        _require(task.completed_s is None and task.owner == aircraft
                 and time <= self.scenario.evaluation_end_s + TIME_TOL,
                 f"Activity outside task or evaluation lifetime: {key}")
        if kind == "unit_started":
            _require(key not in self.active and aircraft not in self.active_aircraft,
                     f"Concurrent inspection use of task {key} or aircraft {aircraft}")
            _require(_close(progress, task.carried_s) and progress < unit.duration_s,
                     f"Activity started with incorrect carried progress: {key}")
            _require(aircraft in self.held["air"], f"Activity without airborne permission: {key}")
            self.active[key] = _Attempt(key, aircraft, order, time, progress)
            self.active_aircraft[aircraft] = key
            return

        attempt = self.active[key]
        _require(attempt.aircraft_id == aircraft and attempt.order == order,
                 f"Activity closure does not match its start: {key}")
        elapsed = time - attempt.started_s
        _require(_close(progress, attempt.carried_s + elapsed) and elapsed >= -TIME_TOL,
                 f"Activity duration does not match event times: {key}")
        limit = min(unit.max_service_wind_mps, profile.max_flight_wind_mps, profile.max_airspeed_mps)
        _require(self._qualifying_weather(attempt.started_s, time, limit),
                 f"Activity interval crossed nonqualifying weather: {key}")
        if kind == "unit_completed":
            _require(_close(progress, unit.duration_s), f"Incomplete inspection credited: {key}")
            task.units_completed += 1
            task.last_unit_s = time
            task.carried_s = 0.0
        else:
            _require(progress < unit.duration_s + TIME_TOL, f"Interrupted activity exceeded its duration: {key}")
            task.carried_s = progress if self.mode == "accumulating" else 0.0
            task.interruptions += 1
        del self.active[key]
        del self.active_aircraft[aircraft]

    def _qualifying_weather(self, start: float, end: float, limit: float) -> bool:
        if end - start <= TIME_TOL:
            return True
        offset = max(0, bisect_right(self.weather_starts, start) - 1)
        for index in range(offset, len(self.scenario.weather)):
            weather = self.scenario.weather[index]
            if weather.start_s >= end - TIME_TOL:
                return True
            if weather.end_s > start + TIME_TOL and weather.speed_mps > limit + TIME_TOL:
                return False
            if weather.end_s >= end - TIME_TOL:
                return True
        return False

    def _completed(self, time: float, event: Mapping) -> None:
        key = _text(event, "task_id")
        task = self.tasks[key]
        aircraft = _text(event, "aircraft_id")
        _require(task.completed_s is None and task.units_completed == len(self.scenario.templates[task.template_id])
                 and task.last_unit_s is not None and _close(time, task.last_unit_s)
                 and time <= self.scenario.evaluation_end_s + TIME_TOL,
                 f"Task {key} completed without all its required work in the evaluation horizon")
        _require((event.get("stream_id"), _text(event, "asset_id"), _text(event, "template_id"))
                 == (task.stream_id, task.asset_id, task.template_id)
                 and _close(_number(event, "deadline_s"), task.deadline_s),
                 f"Task completion refers to different work: {key}")
        _require(task.owner == aircraft and event.get("abstraction") == self.mode,
                 f"Task completion refers to the wrong aircraft or abstraction: {key}")
        task.completed_s = time
        task.owner = None
        del self.assigned_aircraft[aircraft]
        if task.stream_id is not None:
            _require(self.pending_streams.pop(task.stream_id) == key,
                     f"Wrong periodic task completed: {key}")
            self.completed_by_stream[task.stream_id].append(time)

    def _arrived(self, time: float, event: Mapping) -> None:
        aircraft = _text(event, "aircraft_id")
        task = self.tasks[_text(event, "task_id")]
        asset = _text(event, "asset_id")
        _require(task.owner == aircraft and task.asset_id == asset
                 and aircraft in self.held["air"],
                 f"Arrival does not match an airborne assigned task: {aircraft}")
        if self.sortie_limit is None:
            return
        sortie = self.active_sorties[aircraft]
        _require(aircraft not in self.pending_visits, f"Unreported target visit: {aircraft}")
        sortie["visited_asset_ids"].add(asset)
        _require(len(sortie["visited_asset_ids"]) <= self.sortie_limit,
                 f"Sortie exceeds target limit: {sortie['sortie_id']}")
        self.pending_visits[aircraft] = (asset, time)

    def _sortie(self, kind: str, time: float, event: Mapping) -> None:
        aircraft = _text(event, "aircraft_id")
        identifier = _text(event, "sortie_id")
        _require(self.sortie_limit is not None and aircraft in self.profiles,
                 "Sortie event requires a configured limit and known aircraft")
        if kind == "sortie_started":
            _require(aircraft not in self.active_sorties and aircraft in self.held["air"]
                     and time < self.scenario.evaluation_end_s,
                     f"Concurrent sortie or departure outside dispatch horizon: {aircraft}")
            _require(event.get("max_targets_per_sortie") == self.sortie_limit
                     and all(row["sortie_id"] != identifier for row in self.sorties)
                     and all(row["sortie_id"] != identifier for row in self.active_sorties.values()),
                     f"Duplicate sortie ID or incorrect target limit: {identifier}")
            self.active_sorties[aircraft] = {
                "sortie_id": identifier, "aircraft_id": aircraft,
                "started_s": time, "visited_asset_ids": set(),
            }
            return
        sortie = self.active_sorties[aircraft]
        _require(sortie["sortie_id"] == identifier, f"Sortie ID mismatch: {aircraft}")
        count = event.get("target_count")
        _require(type(count) is int and count == len(sortie["visited_asset_ids"]),
                 f"Sortie target count differs from recorded arrivals: {identifier}")
        if kind == "target_visited":
            _require(self.pending_visits.pop(aircraft, None) == (_text(event, "asset_id"), time),
                     f"Target visit lacks a matching arrival: {identifier}")
            return
        outcome = _text(event, "outcome")
        _require(outcome in {"landed", "failed", "censored"} and aircraft not in self.pending_visits,
                 f"Invalid sortie outcome or unresolved target visit: {identifier}")
        _require(aircraft not in self.held["air"],
                 f"Finished sortie still holds airborne permission: {identifier}")
        _require(event.get("visited_asset_ids") == sorted(sortie["visited_asset_ids"]),
                 f"Sortie target list differs from recorded arrivals: {identifier}")
        self.sorties.append({
            **sortie, "visited_asset_ids": sorted(sortie["visited_asset_ids"]),
            "finished_s": time, "outcome": outcome,
        })
        del self.active_sorties[aircraft]

    def _energy_interval(self, time: float, event: Mapping) -> None:
        aircraft = _text(event, "aircraft_id")
        phase = _text(event, "phase")
        profile = self.profiles[aircraft]
        balance = self.energy[aircraft]
        start, end = _number(event, "start_s"), _number(event, "end_s")
        before, after = _number(event, "energy_before_wh"), _number(event, "energy_after_wh")
        used, charged = _number(event, "consumed_wh"), _number(event, "charged_wh")
        grid = _number(event, "grid_wh")
        external = _number(event, "external_wh") if "external_wh" in event else 0.0
        _require(balance.last_interval_end_s - TIME_TOL <= start <= end + TIME_TOL and _close(end, time),
                 f"Energy interval overlaps another interval or has an invalid endpoint: {aircraft}")
        _require(min(used, charged, grid, external, before, after) >= -ENERGY_TOL
                 and max(before, after) <= profile.battery_usable_wh + ENERGY_TOL
                 and _close(before, balance.current_wh, ENERGY_TOL)
                 and _close(after, before - used + charged, ENERGY_TOL),
                 f"Invalid energy balance or battery capacity: {aircraft}")
        duration = max(0.0, end - start)
        if phase == "charging":
            station = self.scenario.station
            available = min(station.port_input_w * station.charge_efficiency, profile.charge_intake_battery_w)
            target = station.full_charge_soc_fraction * profile.battery_usable_wh
            expected = min(max(0.0, target - before), available * duration / 3600)
            _require(aircraft in self.held["charge"] and _close(used, 0.0, ENERGY_TOL)
                     and _close(external, 0.0, ENERGY_TOL)
                     and _close(charged, expected, ENERGY_TOL)
                     and _close(grid * station.charge_efficiency, charged, ENERGY_TOL),
                     f"Charge interval violates its occupancy or power balance: {aircraft}")
            rates = (0.0, available / 3600, available / (3600 * station.charge_efficiency), 0.0)
        elif phase == "instant_charge":
            target = self.scenario.station.full_charge_soc_fraction * profile.battery_usable_wh
            _require(self.mode == "instant_charge" and _close(duration, 0.0)
                     and _close(used + grid, 0.0, ENERGY_TOL)
                     and _close(external, charged, ENERGY_TOL)
                     and _close(after, max(before, target), ENERGY_TOL),
                     f"Invalid external instantaneous charge: {aircraft}")
            rates = None
        else:
            _require(_close(charged + grid + external, 0.0, ENERGY_TOL),
                     f"Energy added outside charging: {aircraft}")
            power = self._phase_power(aircraft, phase, profile)
            _require(_close(used, min(before, power * duration / 3600), ENERGY_TOL),
                     f"Phase energy does not match configured power: {aircraft} {phase}")
            rates = (power / 3600, 0.0, 0.0, 0.0)
        self._record_energy_window(aircraft, start, end, before,
                                   (used, charged, grid, external), rates)
        if phase in {"transit", "return"}:
            self.movement_seconds += duration
            self.evaluation_movement_seconds += self._evaluation_overlap(start, end)
            self.recovery_movement_seconds += max(
                0.0, min(end, self.scenario.recovery_end_s) - max(start, self.scenario.evaluation_end_s),
            )
        balance.current_wh = after
        balance.consumed_wh += used
        balance.charged_wh += charged
        balance.grid_wh += grid
        balance.external_wh += external
        balance.last_interval_end_s = end

    def _record_energy_window(self, aircraft: str, start: float, end: float, before: float,
                              values: tuple[float, ...], rates: tuple[float, ...] | None) -> None:
        def cumulative(time: float) -> tuple[float, ...]:
            elapsed = min(max(time - start, 0.0), end - start)
            return tuple(min(total, rate * elapsed) for total, rate in zip(values, rates))

        for label, boundary in (
            ("evaluation_start", self.scenario.evaluation_start_s),
            ("evaluation_end", self.scenario.evaluation_end_s),
        ):
            if self.battery_snapshots[label][aircraft] is None and boundary <= end:
                if boundary <= start:
                    stored = before
                else:
                    consumed, charged, _, _ = cumulative(boundary)
                    stored = before - consumed + charged
                self.battery_snapshots[label][aircraft] = stored
        for window, (left, right) in self.window_bounds.items():
            totals = self.window_energy[window][aircraft]
            if rates is None:
                # Boundary impulses belong to the interval on the right.
                inside = left <= start < right or window == "recovery" and start == right
                if inside:
                    for name, value in zip(ENERGY_FIELDS, values):
                        totals[name] += value
            else:
                low, high = max(start, left), min(end, right)
                if high > low:
                    for name, at_high, at_low in zip(ENERGY_FIELDS, cumulative(high), cumulative(low)):
                        totals[name] += at_high - at_low

    def _phase_power(self, aircraft: str, phase: str, profile: AircraftProfile) -> float:
        airborne = {"takeoff", "transit", "service", "return", "landing_wait", "landing",
                    "ready_transit", "ready_service", "ready_return", "continue"}
        if phase in airborne:
            _require(aircraft in self.held["air"], f"Airborne phase without permission: {aircraft} {phase}")
        if phase in {"takeoff", "landing"}:
            _require(aircraft in self.held["pad"], f"Pad operation without permission: {aircraft} {phase}")
        if phase == "service":
            key = self.active_aircraft[aircraft]
            task, attempt = self.tasks[key], self.active[key]
            unit = self.scenario.templates[task.template_id][attempt.order - 1]
            return profile.inspection_base_w * unit.inspection_power_factor
        power = {
            "takeoff": profile.takeoff_w, "transit": profile.cruise_w,
            "return": profile.cruise_w, "landing_wait": profile.hold_w,
            "landing": profile.landing_w, "idle": profile.ground_w,
            "turnaround": profile.ground_w, "charge_wait": profile.ground_w,
            "failed": 0.0, "ground_closed": 0.0, "ready_charge": profile.ground_w,
        }
        if phase in {"ready_transit", "ready_service", "ready_return", "continue"}:
            return profile.hold_w
        _require(phase in power, f"Unknown phase in energy record: {phase}")
        return power[phase]

    def _resource(self, kind: str, time: float, event: Mapping) -> None:
        resource = _text(event, "resource")
        aircraft = _text(event, "aircraft_id")
        _require(aircraft in self.profiles, f"Unknown aircraft for resource {resource}: {aircraft}")
        holders = self.held[resource]
        if kind == "resource_acquired":
            _require(aircraft not in holders and len(holders) < self.capacities[resource],
                     f"Duplicate allocation or capacity exceeded: {resource} {aircraft}")
            holders[aircraft] = time
            self.resource_peak[resource] = max(self.resource_peak[resource], len(holders))
            if resource == "air":
                self.air_allocations[aircraft] += 1
                if self.scenario.evaluation_start_s <= time < self.scenario.evaluation_end_s:
                    self.evaluation_air_allocations[aircraft] += 1
        else:
            _require(aircraft in holders, f"Resource released by non-holder: {resource} {aircraft}")
            start = holders.pop(aircraft)
            self.resource_seconds[resource] += time - start
            self.evaluation_resource_seconds[resource] += self._evaluation_overlap(start, time)
            self.recovery_resource_seconds[resource] += max(
                0.0, min(time, self.scenario.recovery_end_s) - max(start, self.scenario.evaluation_end_s),
            )

    def _queue(self, kind: str, time: float, event: Mapping) -> None:
        queue = _text(event, "queue")
        aircraft = _text(event, "aircraft_id")
        waiting = self.queued[queue]
        _require(aircraft in self.profiles, f"Unknown aircraft in {queue} queue: {aircraft}")
        if kind == "queue_entered":
            _require(all(aircraft not in entries for entries in self.queued.values()),
                     f"Duplicate queue entry: {aircraft}")
            waiting[aircraft] = time
        else:
            start = waiting[aircraft]
            wait = time - start
            _require(_close(_number(event, "wait_s"), wait), f"Incorrect queue wait time: {aircraft}")
            if event.get("reason") == "admitted":
                _require(next(iter(waiting)) == aircraft, f"Admission is not FIFO: {queue} {aircraft}")
            del waiting[aircraft]
            self.queue_wait_seconds[queue] += wait
            self.evaluation_queue_wait_seconds[queue] += self._evaluation_overlap(start, time)

    def _weather(self, kind: str, time: float, event: Mapping) -> None:
        start = _number(event, "valid_from_s")
        weather = self.weather_by_start[start]
        _require(_close(_number(event, "wind_east_mps"), weather.east_mps)
                 and _close(_number(event, "wind_north_mps"), weather.north_mps),
                 f"Weather event values differ from input interval: {start:g}s")
        if kind == "weather_changed":
            _require(_close(time, start) and start not in self.world_weather_starts,
                     f"World weather interval is mistimed or duplicated: {start:g}s")
            self.world_weather_starts.add(start)
        else:
            _require(start > self.last_observed_start
                     and _close(time, max(start, weather.visible_at_s))
                     and _close(time, _number(event, "visible_at_s")),
                     f"Weather observation is mistimed or reveals future information: {start:g}s")
            self.last_observed_start = start

    def _aircraft_terminal(self, event: Mapping) -> None:
        aircraft = _text(event, "aircraft_id")
        energy = _number(event, "energy_wh")
        _require(aircraft not in self.terminal and _close(self.energy[aircraft].current_wh, energy, ENERGY_TOL),
                 f"Duplicate terminal event or unbalanced terminal energy: {aircraft}")
        self.terminal[aircraft] = event

    def finish(self) -> dict:
        limits = {
            "run_started": 0.0,
            "evaluation_closed": self.scenario.evaluation_end_s,
            "run_finished": self.scenario.recovery_end_s,
        }
        for kind, expected in limits.items():
            if kind not in self.markers or not _close(self.markers[kind], expected):
                self.errors.append(f"Missing or incorrectly timed run marker: {kind}")
        if self.world_weather_starts != self.weather_by_start.keys():
            self.errors.append("Missing world weather intervals")
        for aircraft in sorted(set(self.profiles) - set(self.terminal)):
            self.errors.append(f"Aircraft terminal event missing: {aircraft}")
        for resource, holders in self.held.items():
            if holders:
                self.errors.append(f"Resource not closed at run end: {resource} {sorted(holders)}")
        for task_id in self.active:
            self.errors.append(f"Inspection activity not closed at run end: {task_id}")
        for aircraft, task_id in self.assigned_aircraft.items():
            self.errors.append(f"Task ownership not closed at run end: {aircraft} {task_id}")
        for queue, waiting in self.queued.items():
            if waiting:
                self.errors.append(f"Queue not closed at run end: {queue} {sorted(waiting)}")
        if self.active_sorties or self.pending_visits:
            self.errors.append("Configured sortie or target visit not closed at run end")
        if self.sortie_limit is not None and len(self.sorties) != sum(self.air_allocations.values()):
            self.errors.append("Configured sortie starts do not match airborne resource allocations")
        for task_id in sorted(set(self.batch) - set(self.tasks)):
            self.errors.append(f"Batch task release missing: {task_id}")
        for stream_id, stream in self.streams.items():
            completions = self.completed_by_stream[stream_id]
            last = completions[-1] if completions else -stream.initial_age_s
            release = max(0.0, last + self.periodic_release_fraction * stream.target_interval_s)
            if stream_id not in self.pending_streams and release < self.scenario.evaluation_end_s - TIME_TOL:
                self.errors.append(f"Periodic task release missing: {stream_id}")
        energy_windows, energy_summary, battery_snapshots = self._energy_summary()
        metrics = self._metrics()
        diagnostics = {key: dict(values) for key, values in self.diagnostics.items()
                       if key != "interruptions"}
        diagnostics["unit_interruptions"] = [
            {"template_id": template, "reason": reason, "count": count}
            for (template, reason), count in sorted(self.diagnostics["interruptions"].items())
        ]
        evaluation_sorties = [
            row for row in self.sorties
            if self.scenario.evaluation_start_s <= row["started_s"] < self.scenario.evaluation_end_s
        ]
        return {
            "status": "fail" if self.errors else "pass",
            "errors": self.errors,
            "scenario_id": self.scenario.scenario_id,
            "execution_mode": self.mode,
            "event_count": self.event_count,
            "metrics": metrics,
            "tasks": {
                key: {"asset_id": task.asset_id, "template_id": task.template_id,
                      "stream_id": task.stream_id, "released_s": task.released_s,
                      "deadline_s": task.deadline_s, "units_completed": task.units_completed,
                      "interruptions": task.interruptions, "completed_s": task.completed_s}
                for key, task in self.tasks.items()
            },
            "energy": {
                aircraft: {"initial_wh": balance.initial_wh, "terminal_wh": balance.current_wh,
                           "consumed_wh": balance.consumed_wh, "charged_wh": balance.charged_wh,
                           "grid_wh": balance.grid_wh, "external_wh": balance.external_wh}
                for aircraft, balance in self.energy.items()
            },
            "evaluation_energy": energy_windows["evaluation"],
            "recovery_energy": energy_windows["recovery"],
            "energy_summary": energy_summary,
            "battery_snapshots_wh": battery_snapshots,
            "operating_metrics": {
                "sorties_started": sum(self.air_allocations.values()),
                "evaluation_sorties_started": sum(self.evaluation_air_allocations.values()),
                "active_aircraft_count": sum(count > 0 for count in self.air_allocations.values()),
                "evaluation_active_aircraft_count": sum(
                    count > 0 for count in self.evaluation_air_allocations.values()
                ),
                "movement_seconds": self.movement_seconds,
                "evaluation_movement_seconds": self.evaluation_movement_seconds,
                "recovery_movement_seconds": self.recovery_movement_seconds,
                "evaluation_airborne_seconds": self.evaluation_resource_seconds["air"],
                "recovery_airborne_seconds": self.recovery_resource_seconds["air"],
                "operation_airborne_seconds": (
                    self.evaluation_resource_seconds["air"] + self.recovery_resource_seconds["air"]
                ),
                "aircraft_failure_count_all": sum(self.all_failures.values()),
                "censored_aircraft_count": sum(
                    event.get("reason") == "censored" for event in self.terminal.values()
                ),
                "configured_sortie_count": len(self.sorties),
                "mean_targets_per_sortie": (
                    sum(len(row["visited_asset_ids"]) for row in self.sorties) / len(self.sorties)
                    if self.sorties else None
                ),
                "max_targets_per_sortie_observed": (
                    max(len(row["visited_asset_ids"]) for row in self.sorties) if self.sorties else None
                ),
                "evaluation_mean_targets_per_sortie": (
                    sum(len(row["visited_asset_ids"]) for row in evaluation_sorties) / len(evaluation_sorties)
                    if evaluation_sorties else None
                ),
            },
            "aircraft_failures_all": dict(self.all_failures),
            "sorties": self.sorties,
            "resource_peak": self.resource_peak,
            "resource_seconds": self.resource_seconds,
            "queue_wait_seconds": self.queue_wait_seconds,
            "evaluation_resource_seconds": self.evaluation_resource_seconds,
            "recovery_resource_seconds": self.recovery_resource_seconds,
            "evaluation_queue_wait_seconds": self.evaluation_queue_wait_seconds,
            "evaluation_diagnostics": diagnostics,
        }

    def _energy_summary(self) -> tuple[dict, dict, dict]:
        for aircraft, balance in self.energy.items():
            for snapshot in self.battery_snapshots.values():
                if snapshot[aircraft] is None:
                    snapshot[aircraft] = balance.current_wh
        snapshots = {
            **self.battery_snapshots,
            "recovery_end": {aircraft: balance.current_wh for aircraft, balance in self.energy.items()},
        }
        summary = {
            f"battery_{label}_wh": sum(values.values()) for label, values in snapshots.items()
        }
        windows = {}
        for window, (start, end) in self.window_bounds.items():
            totals = {
                field: sum(member[field] for member in self.window_energy[window].values())
                for field in ENERGY_FIELDS
            }
            windows[window] = {
                "start_s": start, "end_s": end,
                "by_aircraft": self.window_energy[window], "totals": totals,
            }
            summary.update({f"{window}_{field}": value for field, value in totals.items()})
        for field in ENERGY_FIELDS:
            summary[f"operation_{field}"] = sum(
                self.window_energy[window][aircraft][field]
                for window in self.window_bounds for aircraft in self.energy
            )
        for window, (left, right) in {
            "evaluation": ("evaluation_start", "evaluation_end"),
            "recovery": ("evaluation_end", "recovery_end"),
        }.items():
            for aircraft, values in self.window_energy[window].items():
                residual = (snapshots[left][aircraft] - values["consumed_wh"]
                            + values["charged_wh"] - snapshots[right][aircraft])
                if not _close(residual, 0.0, ENERGY_TOL):
                    self.errors.append(f"Window energy balance differs at {window}: {aircraft}")
        return windows, summary, snapshots

    def _metrics(self) -> dict:
        start, end = self.scenario.evaluation_start_s, self.scenario.evaluation_end_s
        completed = [task for task in self.tasks.values()
                     if task.completed_s is not None and start <= task.completed_s <= end + TIME_TOL]
        metrics = {"effective_completions": len(completed)}
        by_template = {
            key: {"effective_completions": count}
            for key, count in Counter(task.template_id for task in completed).items()
        }
        if self.scenario.business == "batch":
            eligible = [task for task in self.batch.values() if start <= task.deadline_s <= end]
            on_time = sum(
                task.task_id in self.tasks and self.tasks[task.task_id].completed_s is not None
                and self.tasks[task.task_id].completed_s <= task.deadline_s + TIME_TOL
                for task in eligible
            )
            metrics.update(
                eligible_batch_tasks=len(eligible),
                on_time_batch_tasks=on_time,
                on_time_completion_rate=on_time / len(eligible) if eligible else None,
            )
            for template, total in sorted(Counter(task.template_id for task in eligible).items()):
                hits = sum(
                    task.template_id == template
                    and task.task_id in self.tasks and self.tasks[task.task_id].completed_s is not None
                    and self.tasks[task.task_id].completed_s <= task.deadline_s + TIME_TOL
                    for task in eligible
                )
                by_template.setdefault(template, {"effective_completions": 0}).update(
                    eligible_batch_tasks=total, on_time_batch_tasks=hits, on_time_completion_rate=hits / total
                )
        else:
            exposure = {}
            for key, stream in self.streams.items():
                exposure[key] = _overdue_exposure(
                    self.completed_by_stream[key], -stream.initial_age_s,
                    stream.target_interval_s, start, end,
                )
            metrics.update(
                periodic_stream_count=len(exposure),
                standardized_overdue_exposure=(
                    sum(exposure.values()) / ((end - start) * len(exposure)) if exposure else None
                ),
                stream_overdue_exposure_s=exposure,
            )
            for template, total in sorted(Counter(stream.template_id for stream in self.streams.values()).items()):
                area = sum(exposure[key] for key, stream in self.streams.items() if stream.template_id == template)
                by_template.setdefault(template, {"effective_completions": 0}).update(
                    periodic_stream_count=total, standardized_overdue_exposure=area / ((end - start) * total)
                )
            age_metrics = periodic_age_metrics(
                self.streams.values(), self.completed_by_stream, start, end,
            )
            for template, values in age_metrics.pop("by_template").items():
                by_template.setdefault(template, {"effective_completions": 0}).update(values)
            metrics.update(age_metrics)
        metrics["by_template"] = by_template
        return metrics


def periodic_age_metrics(streams: Iterable[PeriodicStream],
                         completed_by_stream: Mapping[str, Iterable[float]],
                         start: float, end: float) -> dict:
    """Integrate report-acceptance age, retaining warm-up updates and unserved streams."""
    _require(math.isfinite(start) and math.isfinite(end) and 0 <= start < end,
             "Age assessment requires a finite positive interval")
    details = {}
    templates = {}
    for stream in streams:
        key = stream.task_stream_id
        _require(key not in details and math.isfinite(stream.target_interval_s)
                 and math.isfinite(stream.initial_age_s)
                 and stream.target_interval_s > 0 and stream.initial_age_s >= 0,
                 f"Duplicate stream or invalid age parameters: {key}")
        times = list(completed_by_stream.get(key, ()))
        _require(all(type(time) in (int, float) and math.isfinite(time) and time >= 0 for time in times),
                 f"Invalid completion time for stream {key}")
        times.sort()
        _require(all(left < right for left, right in zip(times, times[1:])),
                 f"Duplicate completion time for stream {key}")
        cursor, last = start, -stream.initial_age_s
        area = maximum = 0.0
        updates = 0
        for time in times:
            if time < start:
                last = time
            elif time <= end:
                if time > cursor:
                    area += (time - cursor) * ((cursor - last) + (time - last)) / 2
                    maximum = max(maximum, time - last)
                cursor, last = time, time
                updates += 1
            else:
                break
        if end > cursor:
            area += (end - cursor) * ((cursor - last) + (end - last)) / 2
            maximum = max(maximum, end - last)
        average = area / (end - start)
        row = {
            "asset_id": stream.asset_id, "template_id": stream.template_id,
            "target_interval_s": stream.target_interval_s,
            "mean_inspection_age_s": average,
            "normalized_mean_inspection_age": average / stream.target_interval_s,
            "max_inspection_age_s": maximum, "terminal_inspection_age_s": end - last,
            "evaluation_updates": updates,
        }
        details[key] = row
        templates.setdefault(stream.template_id, []).append(row)

    def summarize(rows: list[dict]) -> dict:
        count = len(rows)
        normalized = sorted(row["normalized_mean_inspection_age"] for row in rows)
        return {
            "mean_inspection_age_s": sum(row["mean_inspection_age_s"] for row in rows) / count if count else None,
            "mean_normalized_inspection_age": sum(normalized) / count if count else None,
            "q90_normalized_mean_inspection_age": normalized[math.ceil(0.9 * count) - 1] if count else None,
            "max_inspection_age_s": max(row["max_inspection_age_s"] for row in rows) if count else None,
            "zero_update_stream_count": sum(row["evaluation_updates"] == 0 for row in rows),
        }

    return {
        **summarize(list(details.values())), "stream_inspection_age": details,
        "by_template": {template: summarize(rows) for template, rows in templates.items()},
    }


def _overdue_exposure(completions: Iterable[float], last: float, target: float,
                      start: float, end: float) -> float:
    cursor = start
    area = 0.0
    for time in completions:
        if time < start:
            last = time
        elif time <= end:
            area += _overdue_area(cursor, time, last, target)
            cursor, last = time, time
    return area + _overdue_area(cursor, end, last, target)


def _overdue_area(start: float, end: float, last: float, target: float) -> float:
    low = max(0.0, start - last - target)
    high = max(0.0, end - last - target)
    return (high - low) * (high + low) / (2 * target)


def audit(scenario: Scenario, events: Iterable[Mapping], execution_mode: str = "full",
          *, periodic_release_fraction: float = 0.5,
          max_targets_per_sortie: int | None = None) -> dict:
    """Independently verify one event stream and compute the main study metrics."""
    if execution_mode not in {"full", "accumulating", "instant_charge"}:
        raise ValueError(f"Unknown execution mode: {execution_mode}")
    if not 0 < periodic_release_fraction <= 1:
        raise ValueError("The periodic release fraction must be in (0, 1].")
    if max_targets_per_sortie is not None and (
        type(max_targets_per_sortie) is not int or max_targets_per_sortie not in {1, 2}
    ):
        raise ValueError("The sortie target limit must be 1, 2 or None.")
    checker = _Audit(scenario, execution_mode, periodic_release_fraction, max_targets_per_sortie)
    for event in events:
        checker.read(event)
    return checker.finish()
