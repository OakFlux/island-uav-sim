"""Deterministic task-level execution shared by every dispatch policy.

The calendar advances physical state to each boundary before applying events.
Aircraft, resource occupancy and future weather never pass to a policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from heapq import heappop, heappush
from itertools import count
from math import hypot, isfinite
from typing import TYPE_CHECKING, Any

from .domain import AircraftProfile, Candidate, Observation, PeriodicStream, RunConfig, Scenario
from .physics import ground_speed, leg_time

if TYPE_CHECKING:
    from .ledger import EventLedger
    from .policies import DecisionPolicy

EPS = 1e-8
AIRBORNE = frozenset({
    "takeoff", "ready_transit", "transit", "ready_service", "service",
    "continue", "return", "ready_return", "landing_wait", "landing",
})
MOVING = frozenset({"transit", "return"})
FINISH, ENERGY, EXTERNAL = range(3)


@dataclass
class _Job:
    task_id: str
    asset_id: str
    template_id: str
    deadline_s: float
    priority_weight: float
    stream_id: str | None = None
    owner: str | None = None
    completed_units: int = 0
    progress_s: float = 0.0


@dataclass
class _Aircraft:
    aircraft_id: str
    profile: AircraftProfile
    energy_wh: float
    node: str
    position: tuple[float, float]
    phase: str = "idle"
    stamp: int = 0
    due_s: float | None = None
    job_id: str | None = None
    destination: str | None = None
    speed_mps: float = 0.0
    queue_since_s: float | None = None
    hold_expired: bool = False
    failure: str | None = None
    sortie_number: int = 0
    sortie_id: str | None = None
    visited_assets: set[str] = field(default_factory=set)


class Simulation:
    def __init__(
        self, scenario: Scenario, config: RunConfig, policy: DecisionPolicy, ledger: EventLedger,
    ) -> None:
        self.scenario = scenario
        self.config = config
        self.policy = policy
        self.ledger = ledger
        self.station = scenario.station
        self.base = scenario.base_id
        self.t = 0.0
        self.jobs: dict[str, _Job] = {}
        self.streams = {stream.task_stream_id: stream for stream in scenario.periodic_streams}
        self.stream_last = {stream_id: -stream.initial_age_s for stream_id, stream in self.streams.items()}
        self.stream_count = {stream_id: 0 for stream_id in self.streams}
        self.aircraft = {
            member.aircraft_id: _Aircraft(
                member.aircraft_id, scenario.profiles[member.profile_id],
                member.initial_soc_fraction * scenario.profiles[member.profile_id].battery_usable_wh,
                self.base, scenario.positions[self.base],
            )
            for member in sorted(scenario.fleet, key=lambda value: value.aircraft_id)
        }
        self.holders: dict[str, set[str]] = {"air": set(), "pad": set(), "charge": set()}
        self.capacities = {
            "air": self.station.air_capacity,
            "pad": self.station.pad_capacity,
            "charge": self.station.charge_ports,
        }
        self.landing: list[str] = []
        self.charging: list[str] = []
        self.world_index = -1
        self.visible_index = -1
        self.visible_history: list[tuple[float, float]] = []
        self.closed = False
        self._started = False
        self._events: list[tuple[float, int, int, str, Any]] = []
        self._sequence = count()

    def run(self) -> None:
        if self._started:
            raise RuntimeError("A simulation can only be executed once")
        self._started = True
        sortie_settings = {} if self.config.max_targets_per_sortie is None else {
            "max_targets_per_sortie": self.config.max_targets_per_sortie,
        }
        self._emit("run_started", scenario_id=self.scenario.scenario_id,
                   execution_mode=self.config.execution_mode, policy=self.config.policy,
                   planning_mode=self.config.planning_mode,
                   periodic_release_fraction=self.config.periodic_release_fraction, **sortie_settings)
        self._prepare_calendar()
        while self._events:
            # Canceled stage ends must not become extra dispatch decision times.
            while self._events and not self._current(self._events[0]):
                heappop(self._events)
            if not self._events:
                break
            event_time = self._events[0][0]
            if event_time > self.scenario.recovery_end_s:
                break
            self._advance(event_time)
            dispatch_requested = False
            while self._events and self._events[0][0] == self.t:
                event = heappop(self._events)
                if self._current(event):
                    dispatch_requested = self._handle(event[3], event[4]) or dispatch_requested
            if self.t == self.scenario.recovery_end_s:
                self._finish_run()
                return
            self._settle(dispatch_requested)
        raise RuntimeError("Calendar ended without a recovery boundary")

    def _prepare_calendar(self) -> None:
        for index, interval in enumerate(self.scenario.weather):
            if interval.end_s <= 0 or interval.start_s >= self.scenario.recovery_end_s:
                continue
            self._schedule(max(0.0, interval.start_s), EXTERNAL, "weather", index)
            self._schedule(max(0.0, interval.start_s, interval.visible_at_s), EXTERNAL, "observe", index)
        for task in self.scenario.batch_tasks:
            self._schedule(task.release_s, EXTERNAL, "release", _Job(
                task.task_id, task.asset_id, task.template_id, task.deadline_s, task.priority_weight,
            ))
            self._schedule(task.deadline_s, EXTERNAL, "deadline", task.task_id)
        for stream in self.scenario.periodic_streams:
            self._next_revisit(stream)
        for uav in self.aircraft.values():
            self._phase(uav, "ready_charge")
        self._schedule(self.scenario.evaluation_end_s, EXTERNAL, "close")
        self._schedule(self.scenario.recovery_end_s, EXTERNAL, "end")

    def _schedule(self, time_s: float, priority: int, kind: str, payload: Any = None) -> None:
        if not isfinite(time_s) or time_s < self.t:
            raise ValueError(f"Invalid {kind} time {time_s} at {self.t}")
        if time_s <= self.scenario.recovery_end_s:
            heappush(self._events, (time_s, priority, next(self._sequence), kind, payload))

    def _emit(self, kind: str, **fields: Any) -> None:
        self.ledger.emit(self.t, kind, **fields)

    def _current(self, event: tuple[float, int, int, str, Any]) -> bool:
        _, _, _, kind, payload = event
        if kind in {"finish", "empty"}:
            aircraft_id, stamp = payload
            return stamp == self.aircraft[aircraft_id].stamp
        return True

    def _handle(self, kind: str, payload: Any) -> bool:
        if kind in {"finish", "empty"}:
            aircraft_id, _ = payload
            uav = self.aircraft[aircraft_id]
            if kind == "finish":
                self._complete_phase(uav)
            else:
                self._fail(uav, "energy_depleted")
        elif kind == "weather":
            previous_phases = tuple(uav.phase for uav in self.aircraft.values())
            self.world_index = payload
            weather = self.scenario.weather[payload]
            self._emit("weather_changed", wind_east_mps=weather.east_mps,
                       wind_north_mps=weather.north_mps, valid_from_s=weather.start_s)
            # Adjacent phase transitions are prepared only after same-time weather is applied.
            self._weather_changed()
            # An unpublished environmental boundary is not a policy wake-up signal.
            return previous_phases != tuple(uav.phase for uav in self.aircraft.values())
        elif kind == "observe":
            if payload <= self.visible_index:
                return False
            self.visible_index = payload
            weather = self.scenario.weather[payload]
            self.visible_history.append((self.t, weather.speed_mps))
            self._emit("observation_weather", wind_east_mps=weather.east_mps,
                       wind_north_mps=weather.north_mps, visible_at_s=self.t,
                       valid_from_s=weather.start_s)
        elif kind == "release":
            if not self.closed and self.t < self.scenario.evaluation_end_s:
                self.jobs[payload.task_id] = payload
                self._emit("task_released", **self._task_fields(payload))
        elif kind == "deadline":
            if not self.closed and payload in self.jobs:
                self._emit("task_deadline", **self._task_fields(self.jobs[payload]))
        elif kind == "close":
            self.closed = True
            self._emit("evaluation_closed", pending_tasks=len(self.jobs))
            for uav in self.aircraft.values():
                if uav.phase in {"takeoff", "ready_transit", "transit", "ready_service", "service", "continue"}:
                    self._request_recovery(uav, "evaluation_closed")
                elif uav.phase in {"idle", "ready_charge", "turnaround", "charge_wait", "charging"}:
                    self._stop_ground_work(uav)
        return True

    @staticmethod
    def _task_fields(job: _Job) -> dict[str, Any]:
        return {
            "task_id": job.task_id, "asset_id": job.asset_id, "template_id": job.template_id,
            "stream_id": job.stream_id, "deadline_s": job.deadline_s,
        }

    def _next_revisit(self, stream: PeriodicStream) -> None:
        stream_id = stream.task_stream_id
        self.stream_count[stream_id] += 1
        last = self.stream_last[stream_id]
        release = max(self.t, last + self.config.periodic_release_fraction * stream.target_interval_s)
        task_id = f"{stream_id}:{self.stream_count[stream_id]}"
        deadline = last + stream.target_interval_s
        self._schedule(release, EXTERNAL, "release", _Job(
            task_id, stream.asset_id, stream.template_id, deadline, stream.priority_weight, stream_id,
        ))
        if deadline >= self.t:
            self._schedule(deadline, EXTERNAL, "deadline", task_id)

    @property
    def _world_wind(self) -> tuple[float, float]:
        weather = self.scenario.weather[self.world_index]
        return weather.east_mps, weather.north_mps

    def _power(self, uav: _Aircraft) -> float:
        profile = uav.profile
        if uav.phase in MOVING:
            return profile.cruise_w
        if uav.phase == "service":
            job = self.jobs[uav.job_id]
            unit = self.scenario.templates[job.template_id][job.completed_units]
            return profile.inspection_base_w * unit.inspection_power_factor
        if uav.phase == "takeoff":
            return profile.takeoff_w
        if uav.phase == "landing":
            return profile.landing_w
        if uav.phase in AIRBORNE:
            return profile.hold_w
        if uav.phase in {"charging", "failed", "ground_closed"}:
            return 0.0
        return profile.ground_w

    def _battery_power(self, uav: _Aircraft) -> float:
        return min(self.station.port_input_w * self.station.charge_efficiency, uav.profile.charge_intake_battery_w)

    def _charge_target(self, uav: _Aircraft) -> float:
        return self.station.full_charge_soc_fraction * uav.profile.battery_usable_wh

    def _phase(self, uav: _Aircraft, phase: str, duration_s: float | None = None) -> None:
        old_phase = uav.phase
        uav.phase = phase
        uav.stamp += 1
        uav.due_s = None if duration_s is None else self.t + max(0.0, duration_s)
        if old_phase != phase:
            self._emit("phase_changed", aircraft_id=uav.aircraft_id, old_phase=old_phase,
                       phase=phase, energy_wh=uav.energy_wh)
        identity = (uav.aircraft_id, uav.stamp)
        if uav.due_s is not None:
            self._schedule(uav.due_s, FINISH, "finish", identity)
        power = self._power(uav)
        if power > 0:
            empty_s = self.t + max(0.0, uav.energy_wh) * 3600 / power
            if uav.due_s is None or empty_s <= uav.due_s:
                self._schedule(empty_s, ENERGY, "empty", identity)

    def _advance(self, time_s: float) -> None:
        elapsed = time_s - self.t
        if elapsed <= 0.0:
            return
        for uav in self.aircraft.values():
            if uav.phase == "failed":
                continue
            before = uav.energy_wh
            consumed = self._power(uav) * elapsed / 3600
            charged = 0.0
            grid = 0.0
            distance = 0.0
            if uav.phase == "charging":
                charged = min(max(0.0, self._charge_target(uav) - before),
                              self._battery_power(uav) * elapsed / 3600)
                grid = charged / self.station.charge_efficiency
            elif uav.phase in MOVING:
                x, y = uav.position
                target_x, target_y = self.scenario.positions[uav.destination]
                total = hypot(target_x - x, target_y - y)
                distance = min(total, uav.speed_mps * elapsed)
                if total:
                    fraction = distance / total
                    uav.position = (x + (target_x - x) * fraction, y + (target_y - y) * fraction)
            elif uav.phase == "service":
                self.jobs[uav.job_id].progress_s += elapsed
            after = before - consumed + charged
            if after < -EPS:
                raise RuntimeError(f"{uav.aircraft_id} crossed zero energy before its event")
            uav.energy_wh = max(0.0, after)
            self.ledger.emit(time_s, "energy_interval", aircraft_id=uav.aircraft_id,
                             start_s=self.t, end_s=time_s, phase=uav.phase,
                             energy_before_wh=before, energy_after_wh=uav.energy_wh,
                             consumed_wh=consumed, charged_wh=charged, grid_wh=grid,
                             external_wh=0.0, distance_m=distance)
        self.t = time_s

    def _complete_phase(self, uav: _Aircraft) -> None:
        phase = uav.phase
        if phase == "takeoff":
            self._release_resource("pad", uav)
            self._phase(uav, "ready_transit")
        elif phase in MOVING:
            uav.node = uav.destination
            uav.position = self.scenario.positions[uav.node]
            uav.destination = None
            if phase == "transit":
                self._emit("arrived", aircraft_id=uav.aircraft_id, **self._task_fields(self.jobs[uav.job_id]))
                self._visit_target(uav)
                self._phase(uav, "ready_service")
            elif uav.node == self.base:
                self._phase(uav, "landing_wait")
                self._enter_queue(uav, "landing")
                uav.hold_expired = False
                self._phase(uav, "landing_wait", self.station.maximum_landing_hold_s)
            else:
                self._phase(uav, "ready_return")
        elif phase == "service":
            self._complete_unit(uav)
        elif phase == "landing_wait":
            uav.hold_expired = True
        elif phase == "landing":
            self._release_resource("pad", uav)
            self._release_resource("air", uav)
            self._finish_sortie(uav, "landed")
            if self.closed:
                self._phase(uav, "ground_closed")
            else:
                self._phase(uav, "turnaround", self.station.turnaround_s)
        elif phase == "turnaround":
            self._phase(uav, "ready_charge")
        elif phase == "charging":
            self._release_resource("charge", uav)
            self._phase(uav, "idle")

    def _complete_unit(self, uav: _Aircraft) -> None:
        job = self.jobs[uav.job_id]
        units = self.scenario.templates[job.template_id]
        unit = units[job.completed_units]
        self._emit("unit_completed", aircraft_id=uav.aircraft_id, unit_order=unit.order,
                   progress_s=unit.duration_s, **self._task_fields(job))
        job.completed_units += 1
        job.progress_s = 0.0
        if job.completed_units < len(units):
            self._phase(uav, "ready_service")
            return
        self._emit("task_completed", aircraft_id=uav.aircraft_id,
                   abstraction=self.config.execution_mode, **self._task_fields(job))
        del self.jobs[job.task_id]
        uav.job_id = None
        if job.stream_id is not None:
            self.stream_last[job.stream_id] = self.t
            self._next_revisit(self.streams[job.stream_id])
        self._phase(uav, "continue")

    def _interrupt(self, uav: _Aircraft, reason: str) -> None:
        if uav.job_id is None:
            return
        job = self.jobs[uav.job_id]
        if uav.phase == "service":
            unit = self.scenario.templates[job.template_id][job.completed_units]
            self._emit("unit_interrupted", aircraft_id=uav.aircraft_id, unit_order=unit.order,
                       progress_s=job.progress_s, reason=reason, **self._task_fields(job))
            if self.config.execution_mode != "accumulating":
                job.progress_s = 0.0
        self._emit("task_interrupted", aircraft_id=uav.aircraft_id, reason=reason, **self._task_fields(job))
        job.owner = None
        uav.job_id = None

    def _permitted_leg(self, origin: str, destination: str) -> bool:
        return origin == destination or tuple(sorted((origin, destination))) in self.scenario.allowed_legs

    def _route_time(
        self, uav: _Aircraft, origin: tuple[float, float], destination: str, wind: tuple[float, float],
    ) -> float | None:
        return leg_time(uav.profile, origin, self.scenario.positions[destination], *wind)

    @staticmethod
    def _hover_wind_limit(profile: AircraftProfile) -> float:
        return min(profile.max_flight_wind_mps, profile.max_airspeed_mps)

    def _start_motion(self, uav: _Aircraft, destination: str, phase: str) -> None:
        position = self.scenario.positions[destination]
        distance = hypot(position[0] - uav.position[0], position[1] - uav.position[1])
        uav.destination = destination
        if distance <= EPS:
            uav.speed_mps = 0.0
            self._phase(uav, phase, 0.0)
            return
        speed = ground_speed(uav.profile, uav.position, position, *self._world_wind)
        if speed is None:
            raise RuntimeError(f"{uav.aircraft_id}: motion started without a feasible wind check")
        uav.speed_mps = speed
        self._phase(uav, phase, distance / speed)

    def _request_recovery(self, uav: _Aircraft, reason: str) -> None:
        self._interrupt(uav, reason)
        endpoints = {uav.node}
        if uav.phase in MOVING and uav.destination is not None:
            endpoints.add(uav.destination)
        if uav.phase == "takeoff":
            self._release_resource("pad", uav)
        candidates = []
        wind = self._world_wind
        for node in sorted(endpoints):
            if not self._permitted_leg(node, self.base):
                continue
            to_endpoint = self._route_time(uav, uav.position, node, wind)
            onward = self._route_time(uav, self.scenario.positions[node], self.base, wind)
            if to_endpoint is not None and onward is not None:
                candidates.append((to_endpoint + onward, node))
        if not candidates:
            self._fail(uav, "no_feasible_recovery_leg")
            return
        flight_s, endpoint = min(candidates)
        required_wh = (flight_s * uav.profile.cruise_w + self.station.landing_s * uav.profile.landing_w) / 3600
        if uav.energy_wh + EPS < required_wh + uav.profile.return_reserve_wh:
            self._emit("recovery_reserve_shortfall", aircraft_id=uav.aircraft_id,
                       energy_wh=uav.energy_wh, required_wh=required_wh + uav.profile.return_reserve_wh)
        self._emit("recovery_requested", aircraft_id=uav.aircraft_id, reason=reason, via_node=endpoint)
        self._start_motion(uav, endpoint, "return")

    def _weather_changed(self) -> None:
        wind = self._world_wind
        wind_speed = hypot(*wind)
        for uav in self.aircraft.values():
            phase = uav.phase
            if phase in {"takeoff", "service"} and wind_speed > self._hover_wind_limit(uav.profile) + EPS:
                self._request_recovery(uav, "flight_weather_closed")
            elif phase in {"landing", "landing_wait"} and wind_speed > self._hover_wind_limit(uav.profile) + EPS:
                self._fail(uav, "landing_weather_closed")
            elif phase == "transit":
                job = self.jobs[uav.job_id]
                candidate, reason = self._assess(uav, job, uav.node, uav.position, uav.energy_wh, wind, False)
                if candidate is None:
                    self._request_recovery(uav, reason)
                else:
                    self._start_motion(uav, uav.destination, "transit")
            elif phase == "return":
                if self._route_time(uav, uav.position, uav.destination, wind) is None:
                    self._request_recovery(uav, "return_weather_changed")
                else:
                    self._start_motion(uav, uav.destination, "return")
            elif phase == "service":
                job = self.jobs[uav.job_id]
                _, reason = self._assess(uav, job, uav.node, uav.position, uav.energy_wh, wind, False)
                if reason is not None:
                    self._request_recovery(uav, reason)

    def _open_age(self, limit: float) -> float:
        start = self.t
        for visible_at, speed in reversed(self.visible_history):
            if speed > limit + EPS:
                break
            start = visible_at
        return self.t - start

    def _charging_wait(self, projected_arrival_s: float) -> float:
        if self.config.execution_mode == "instant_charge":
            return 0.0
        free_at = [self.aircraft[holder].due_s for holder in self.holders["charge"]]
        free_at.extend([self.t] * (self.station.charge_ports - len(free_at)))
        for aircraft_id in self.charging:
            waiting = self.aircraft[aircraft_id]
            duration = max(0.0, self._charge_target(waiting) - waiting.energy_wh) * 3600 / self._battery_power(waiting)
            earliest = min(range(len(free_at)), key=free_at.__getitem__)
            free_at[earliest] += duration
        return max(0.0, min(free_at) - projected_arrival_s)

    def _assess(
        self, uav: _Aircraft, job: _Job, origin: str, position: tuple[float, float],
        available_wh: float, wind: tuple[float, float], on_ground: bool,
    ) -> tuple[Candidate | None, str | None]:
        units = self.scenario.templates[job.template_id][job.completed_units:]
        profile = uav.profile
        if any(unit.required_capability not in profile.capabilities for unit in units):
            return None, "capability_mismatch"
        service_limit = min(self._hover_wind_limit(profile), *(unit.max_service_wind_mps for unit in units))
        if hypot(*wind) > service_limit + EPS:
            return None, "service_weather_closed"
        if not self._permitted_leg(origin, job.asset_id) or not self._permitted_leg(job.asset_id, self.base):
            return None, "unavailable_route"
        travel_s = self._route_time(uav, position, job.asset_id, wind)
        return_s = self._route_time(uav, self.scenario.positions[job.asset_id], self.base, wind)
        if travel_s is None or return_s is None:
            return None, "infeasible_wind_leg"
        durations = (max(0.0, units[0].duration_s - job.progress_s), *(unit.duration_s for unit in units[1:]))
        service_wh = sum(seconds * unit.inspection_power_factor
                         for unit, seconds in zip(units, durations)) * profile.inspection_base_w / 3600
        departure_s = self.station.takeoff_s if on_ground else 0.0
        energy_wh = (
            service_wh + ((travel_s + return_s) * profile.cruise_w
                          + departure_s * profile.takeoff_w + self.station.landing_s * profile.landing_w) / 3600
        )
        if available_wh + EPS < energy_wh + profile.return_reserve_wh:
            return None, "insufficient_return_reserve"
        service_s = sum(durations)
        return_s += self.station.landing_s
        projected_charge = self.t + departure_s + travel_s + service_s + return_s + self.station.turnaround_s
        last = None
        interval = None
        overdue_ratio = 0.0
        if job.stream_id is not None:
            last = self.stream_last[job.stream_id]
            interval = self.streams[job.stream_id].target_interval_s
            overdue_ratio = max(0.0, (self.t - last) / interval - 1.0)
        return Candidate(
            task_id=job.task_id, deadline_s=job.deadline_s, priority_weight=job.priority_weight,
            travel_s=departure_s + travel_s, service_s=service_s, return_s=return_s,
            energy_wh=energy_wh, open_age_s=self._open_age(service_limit),
            weather_limit_mps=service_limit, charging_wait_s=self._charging_wait(projected_charge),
            overdue_ratio=overdue_ratio, activity_durations_s=durations, stream_interval_s=interval,
            last_completion_s=last,
        ), None

    def _available_jobs(self) -> list[_Job]:
        return [job for job in self.jobs.values() if job.owner is None]

    def _within_sortie_limit(self, uav: _Aircraft, *assets: str) -> bool:
        limit = self.config.max_targets_per_sortie
        return limit is None or len(uav.visited_assets.union(assets)) <= limit

    def _start_sortie(self, uav: _Aircraft) -> None:
        if self.config.max_targets_per_sortie is None:
            return
        if uav.sortie_id is not None:
            raise RuntimeError(f"{uav.aircraft_id}: departure before the previous sortie ended")
        uav.sortie_number += 1
        uav.sortie_id = f"{uav.aircraft_id}-S{uav.sortie_number:06d}"
        uav.visited_assets.clear()
        self._emit("sortie_started", aircraft_id=uav.aircraft_id, sortie_id=uav.sortie_id,
                   max_targets_per_sortie=self.config.max_targets_per_sortie)

    def _visit_target(self, uav: _Aircraft) -> None:
        if self.config.max_targets_per_sortie is None:
            return
        if uav.sortie_id is None or not self._within_sortie_limit(uav, uav.node):
            raise RuntimeError(f"{uav.aircraft_id}: target arrival violates its sortie limit")
        new_target = uav.node not in uav.visited_assets
        uav.visited_assets.add(uav.node)
        self._emit("target_visited", aircraft_id=uav.aircraft_id, sortie_id=uav.sortie_id,
                   asset_id=uav.node, new_target=new_target, target_count=len(uav.visited_assets),
                   visited_asset_ids=sorted(uav.visited_assets))

    def _finish_sortie(self, uav: _Aircraft, outcome: str) -> None:
        if uav.sortie_id is None:
            return
        self._emit("sortie_finished", aircraft_id=uav.aircraft_id, sortie_id=uav.sortie_id,
                   outcome=outcome, target_count=len(uav.visited_assets),
                   visited_asset_ids=sorted(uav.visited_assets))
        uav.sortie_id = None
        uav.visited_assets.clear()

    def _with_followups(
        self, uav: _Aircraft, initial: Candidate, jobs: list[_Job], wind: tuple[float, float],
    ) -> Candidate:
        first = self.jobs[initial.task_id]
        destination = self.scenario.positions[first.asset_id]
        unused_return_wh = (
            (initial.return_s - self.station.landing_s) * uav.profile.cruise_w
            + self.station.landing_s * uav.profile.landing_w
        ) / 3600
        consumed_wh = initial.energy_wh - unused_return_wh
        followups = []
        for job in jobs:
            if job.task_id == first.task_id:
                continue
            if not self._within_sortie_limit(uav, first.asset_id, job.asset_id):
                continue
            candidate, _ = self._assess(
                uav, job, first.asset_id, destination, uav.energy_wh - consumed_wh, wind, False,
            )
            if candidate is not None:
                followups.append(replace(
                    candidate, energy_wh=consumed_wh + candidate.energy_wh,
                    charging_wait_s=max(0.0, candidate.charging_wait_s - initial.travel_s - initial.service_s),
                ))
        return replace(initial, followups=tuple(followups))

    def _dispatch(self, uav: _Aircraft, on_ground: bool) -> None:
        candidates: list[Candidate] = []
        rejected: dict[str, int] = {}
        visible_wind = None
        if self.visible_index >= 0:
            record = self.scenario.weather[self.visible_index]
            visible_wind = (record.east_mps, record.north_mps)
            jobs = self._available_jobs()
            for job in jobs:
                if not self._within_sortie_limit(uav, job.asset_id):
                    rejected["sortie_target_limit"] = rejected.get("sortie_target_limit", 0) + 1
                    continue
                candidate, reason = self._assess(
                    uav, job, uav.node, uav.position, uav.energy_wh, visible_wind, on_ground,
                )
                if candidate is None:
                    rejected[reason] = rejected.get(reason, 0) + 1
                else:
                    candidates.append(candidate)
            if self.config.policy == "B2":
                candidates = [self._with_followups(uav, candidate, jobs, visible_wind) for candidate in candidates]
        observation = Observation(
            time_s=self.t, aircraft_id=uav.aircraft_id, phase=uav.phase, business=self.scenario.business,
            energy_wh=uav.energy_wh, wind_east_mps=visible_wind[0] if visible_wind else None,
            wind_north_mps=visible_wind[1] if visible_wind else None, candidates=tuple(candidates),
        )
        choice = self.policy.choose(observation)
        reason = None
        if choice is None:
            if visible_wind is None:
                reason = "no_visible_weather"
            else:
                reason = "no_admissible_candidate" if not candidates else "policy_wait_or_return"
        elif choice not in {candidate.task_id for candidate in candidates}:
            reason = "policy_selected_inadmissible_task"
        else:
            _, reason = self._assess(
                uav, self.jobs[choice], uav.node, uav.position, uav.energy_wh, self._world_wind, on_ground,
            )
        self._emit("decision", aircraft_id=uav.aircraft_id, task_id=choice, phase=uav.phase,
                   policy=self.config.policy, planning_mode=self.config.planning_mode,
                   planning_comparison=getattr(self.policy, "planning_comparison", None),
                   accepted=reason is None, reason=reason, visible_wind_uv=visible_wind,
                   visible_at_s=self.visible_history[-1][0] if visible_wind is not None else None,
                   candidate_task_ids=[candidate.task_id for candidate in candidates],
                   candidate_rejections=rejected, candidate_estimates=[
                       {
                           "task_id": candidate.task_id, "travel_s": candidate.travel_s,
                           "service_s": candidate.service_s, "return_s": candidate.return_s,
                           "energy_wh": candidate.energy_wh, "open_age_s": candidate.open_age_s,
                           "charging_wait_s": candidate.charging_wait_s,
                           "followup_ids": [followup.task_id for followup in candidate.followups],
                       }
                       for candidate in candidates
                   ])
        if reason is not None:
            if not on_ground:
                self._request_recovery(uav, reason)
            return
        job = self.jobs[choice]
        job.owner = uav.aircraft_id
        uav.job_id = job.task_id
        self._emit("task_assigned", aircraft_id=uav.aircraft_id, **self._task_fields(job))
        if on_ground:
            self._acquire_resource("air", uav)
            self._acquire_resource("pad", uav)
            self._start_sortie(uav)
            self._phase(uav, "takeoff", self.station.takeoff_s)
        else:
            self._start_motion(uav, job.asset_id, "transit")

    def _prepare_outbound(self, uav: _Aircraft) -> None:
        job = self.jobs[uav.job_id]
        _, reason = self._assess(uav, job, uav.node, uav.position, uav.energy_wh, self._world_wind, False)
        if reason is None:
            self._start_motion(uav, job.asset_id, "transit")
        else:
            self._request_recovery(uav, reason)

    def _start_service(self, uav: _Aircraft) -> None:
        job = self.jobs[uav.job_id]
        _, reason = self._assess(uav, job, uav.node, uav.position, uav.energy_wh, self._world_wind, False)
        if reason is not None:
            self._request_recovery(uav, reason)
            return
        unit = self.scenario.templates[job.template_id][job.completed_units]
        self._emit("unit_started", aircraft_id=uav.aircraft_id, unit_order=unit.order,
                   progress_s=job.progress_s, **self._task_fields(job))
        self._phase(uav, "service", unit.duration_s - job.progress_s)

    def _acquire_resource(self, resource: str, uav: _Aircraft) -> None:
        holders = self.holders[resource]
        if len(holders) >= self.capacities[resource] or uav.aircraft_id in holders:
            raise RuntimeError(f"Resource {resource} unavailable for {uav.aircraft_id}")
        holders.add(uav.aircraft_id)
        self._emit("resource_acquired", resource=resource, aircraft_id=uav.aircraft_id,
                   capacity=self.capacities[resource])

    def _release_resource(self, resource: str, uav: _Aircraft) -> None:
        holders = self.holders[resource]
        if uav.aircraft_id not in holders:
            raise RuntimeError(f"{uav.aircraft_id} released unheld {resource}")
        holders.remove(uav.aircraft_id)
        self._emit("resource_released", resource=resource, aircraft_id=uav.aircraft_id,
                   capacity=self.capacities[resource])

    def _enter_queue(self, uav: _Aircraft, queue: str) -> None:
        if uav.queue_since_s is not None:
            raise RuntimeError(f"{uav.aircraft_id} already holds a queue place")
        uav.queue_since_s = self.t
        waiting = self.landing if queue == "landing" else self.charging
        waiting.append(uav.aircraft_id)
        waiting.sort(key=lambda aircraft_id: (self.aircraft[aircraft_id].queue_since_s, aircraft_id))
        self._emit("queue_entered", aircraft_id=uav.aircraft_id, queue=queue)

    def _leave_queue(self, uav: _Aircraft, queue: str, reason: str) -> None:
        (self.landing if queue == "landing" else self.charging).remove(uav.aircraft_id)
        self._emit("queue_left", aircraft_id=uav.aircraft_id, queue=queue,
                   wait_s=self.t - uav.queue_since_s, reason=reason)
        uav.queue_since_s = None

    def _charge_or_queue(self, uav: _Aircraft) -> None:
        target_wh = self._charge_target(uav)
        if uav.energy_wh >= target_wh - EPS:
            self._phase(uav, "idle")
        elif self.config.execution_mode == "instant_charge":
            before = uav.energy_wh
            uav.energy_wh = target_wh
            self._emit("energy_interval", aircraft_id=uav.aircraft_id, start_s=self.t, end_s=self.t,
                       phase="instant_charge", energy_before_wh=before, energy_after_wh=target_wh,
                       consumed_wh=0.0, charged_wh=target_wh - before, grid_wh=0.0,
                       external_wh=target_wh - before, distance_m=0.0)
            self._phase(uav, "idle")
        else:
            self._enter_queue(uav, "charge")
            self._phase(uav, "charge_wait")

    def _start_charging(self) -> None:
        while self.charging and len(self.holders["charge"]) < self.capacities["charge"]:
            uav = self.aircraft[self.charging[0]]
            self._leave_queue(uav, "charge", "admitted")
            self._acquire_resource("charge", uav)
            duration_s = (self._charge_target(uav) - uav.energy_wh) * 3600 / self._battery_power(uav)
            self._phase(uav, "charging", duration_s)

    def _start_landings(self) -> None:
        while self.landing and len(self.holders["pad"]) < self.capacities["pad"]:
            uav = self.aircraft[self.landing[0]]
            if hypot(*self._world_wind) > self._hover_wind_limit(uav.profile) + EPS:
                self._fail(uav, "landing_weather_closed")
                continue
            if uav.energy_wh + EPS < uav.profile.landing_w * self.station.landing_s / 3600:
                self._fail(uav, "insufficient_landing_energy")
                continue
            self._leave_queue(uav, "landing", "admitted")
            self._acquire_resource("pad", uav)
            self._phase(uav, "landing", self.station.landing_s)
        for aircraft_id in tuple(self.landing):
            uav = self.aircraft[aircraft_id]
            if uav.hold_expired:
                self._fail(uav, "landing_wait_exceeded")

    def _settle(self, dispatch_requested: bool) -> None:
        for uav in self.aircraft.values():
            phase = uav.phase
            if phase == "ready_return":
                if self._route_time(uav, uav.position, self.base, self._world_wind) is None:
                    self._fail(uav, "no_feasible_recovery_leg")
                else:
                    self._start_motion(uav, self.base, "return")
            elif phase == "ready_transit":
                self._prepare_outbound(uav)
            elif phase == "ready_service":
                self._start_service(uav)
            elif phase == "ready_charge":
                self._charge_or_queue(uav)
        if self._events and self._events[0][0] == self.t:
            return  # Process a zero-distance or zero-duration stage before any new decision.
        self._start_landings()
        if not self.closed:
            self._start_charging()
        if self._events and self._events[0][0] == self.t:
            return
        if self.closed or not dispatch_requested:
            return
        for uav in self.aircraft.values():
            if uav.phase == "continue":
                self._dispatch(uav, on_ground=False)
        if self._events and self._events[0][0] == self.t:
            return
        for uav in self.aircraft.values():
            if uav.phase != "idle":
                continue
            if len(self.holders["air"]) == self.capacities["air"] or len(self.holders["pad"]) == self.capacities["pad"]:
                self._emit("dispatch_blocked", aircraft_id=uav.aircraft_id,
                           reason="air_capacity" if len(self.holders["air"]) == self.capacities["air"] else "pad_capacity")
                continue
            self._dispatch(uav, on_ground=True)

    def _stop_ground_work(self, uav: _Aircraft) -> None:
        if uav.phase == "charge_wait":
            self._leave_queue(uav, "charge", "evaluation_closed")
        if uav.phase == "charging":
            self._release_resource("charge", uav)
        self._phase(uav, "ground_closed")

    def _fail(self, uav: _Aircraft, reason: str) -> None:
        self._interrupt(uav, reason)
        if uav.queue_since_s is not None:
            self._leave_queue(uav, "landing" if uav.phase == "landing_wait" else "charge", reason)
        for resource, holders in self.holders.items():
            if uav.aircraft_id in holders:
                self._release_resource(resource, uav)
        uav.failure = reason
        self._phase(uav, "failed")
        self._emit("aircraft_failed", aircraft_id=uav.aircraft_id, reason=reason, energy_wh=uav.energy_wh)
        self._finish_sortie(uav, "failed")

    def _finish_run(self) -> None:
        for uav in self.aircraft.values():
            reason = uav.failure
            if uav.phase in AIRBORNE:
                reason = "censored"
                self._interrupt(uav, "recovery_horizon")
            if uav.queue_since_s is not None:
                self._leave_queue(uav, "landing" if uav.phase == "landing_wait" else "charge", "recovery_horizon")
            for resource, holders in self.holders.items():
                if uav.aircraft_id in holders:
                    self._release_resource(resource, uav)
            self._finish_sortie(uav, "censored")
            self._emit("aircraft_terminal", aircraft_id=uav.aircraft_id, phase=uav.phase,
                       energy_wh=uav.energy_wh, reason=reason, x_m=uav.position[0], y_m=uav.position[1])
        self._emit("run_finished", pending_tasks=len(self.jobs))
