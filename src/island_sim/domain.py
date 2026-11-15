"""Immutable input and decision records. Runtime state belongs to the engine."""
from __future__ import annotations

from dataclasses import dataclass
from math import hypot
from typing import Mapping

Point = tuple[float, float]


@dataclass(frozen=True)
class AircraftProfile:
    profile_id: str
    capabilities: frozenset[str]
    battery_usable_wh: float
    return_reserve_wh: float
    max_airspeed_mps: float
    max_groundspeed_mps: float
    min_groundspeed_mps: float
    max_flight_wind_mps: float
    takeoff_w: float
    cruise_w: float
    inspection_base_w: float
    hold_w: float
    landing_w: float
    ground_w: float
    charge_intake_battery_w: float


@dataclass(frozen=True)
class UnitSpec:
    order: int
    required_capability: str
    duration_s: float
    max_service_wind_mps: float
    inspection_power_factor: float


@dataclass(frozen=True)
class FleetMember:
    aircraft_id: str
    profile_id: str
    initial_soc_fraction: float


@dataclass(frozen=True)
class Station:
    station_setting_id: str
    air_capacity: int
    pad_capacity: int
    charge_ports: int
    port_input_w: float
    charge_efficiency: float
    full_charge_soc_fraction: float
    takeoff_s: float
    landing_s: float
    turnaround_s: float
    maximum_landing_hold_s: float


@dataclass(frozen=True)
class WeatherInterval:
    start_s: float
    end_s: float
    visible_at_s: float
    east_mps: float
    north_mps: float

    @property
    def speed_mps(self) -> float:
        return hypot(self.east_mps, self.north_mps)


@dataclass(frozen=True)
class BatchTask:
    task_id: str
    asset_id: str
    template_id: str
    release_s: float
    deadline_s: float
    priority_weight: float


@dataclass(frozen=True)
class PeriodicStream:
    task_stream_id: str
    asset_id: str
    template_id: str
    target_interval_s: float
    initial_age_s: float
    priority_weight: float


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    business: str
    base_id: str
    positions: Mapping[str, Point]
    allowed_legs: frozenset[tuple[str, str]]
    profiles: Mapping[str, AircraftProfile]
    templates: Mapping[str, tuple[UnitSpec, ...]]
    fleet: tuple[FleetMember, ...]
    station: Station
    weather: tuple[WeatherInterval, ...]
    batch_tasks: tuple[BatchTask, ...]
    periodic_streams: tuple[PeriodicStream, ...]
    evaluation_start_s: float
    evaluation_end_s: float
    recovery_end_s: float
    provenance: Mapping[str, str]


@dataclass(frozen=True)
class RunConfig:
    execution_mode: str = "full"
    policy: str = "B0"
    planning_mode: str = "full"
    periodic_release_fraction: float = 0.5
    max_targets_per_sortie: int | None = None

    def __post_init__(self) -> None:
        if self.execution_mode not in {"full", "accumulating", "instant_charge"}:
            raise ValueError(f"Unknown execution mode: {self.execution_mode}")
        if self.policy not in {"B0", "B1", "B2"}:
            raise ValueError(f"Unknown policy: {self.policy}")
        if self.planning_mode not in {"full", "coarse"}:
            raise ValueError(f"Unknown planning mode: {self.planning_mode}")
        if self.planning_mode == "coarse" and self.policy != "B2":
            raise ValueError("Only B2 has alternate planning estimators.")
        if not 0 < self.periodic_release_fraction <= 1:
            raise ValueError("The periodic release fraction must be in (0, 1].")
        if self.max_targets_per_sortie is not None and (
            type(self.max_targets_per_sortie) is not int or self.max_targets_per_sortie < 1
        ):
            raise ValueError("The target limit per sortie must be a positive integer or None.")


@dataclass(frozen=True)
class Candidate:
    task_id: str
    deadline_s: float
    priority_weight: float
    travel_s: float
    service_s: float
    return_s: float
    energy_wh: float
    open_age_s: float = 0.0
    weather_limit_mps: float = 0.0
    charging_wait_s: float = 0.0
    overdue_ratio: float = 0.0
    followups: tuple[Candidate, ...] = ()
    activity_durations_s: tuple[float, ...] = ()
    stream_interval_s: float | None = None
    last_completion_s: float | None = None


@dataclass(frozen=True)
class Observation:
    time_s: float
    aircraft_id: str
    phase: str
    business: str
    energy_wh: float
    wind_east_mps: float | None
    wind_north_mps: float | None
    candidates: tuple[Candidate, ...]
