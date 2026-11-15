"""Dispatch and planning policies consume only observations and frozen development inputs."""
from __future__ import annotations

from bisect import bisect_left
from pathlib import Path
from typing import Protocol

from .domain import Candidate, Observation, RunConfig, Scenario
from .inputs import InputError, _InputFiles, _field, _number, _utc
from .physics import WIND_TOLERANCE


class DecisionPolicy(Protocol):
    def choose(self, observation: Observation) -> str | None:
        """Return a task ID or leave the aircraft idle (return if already airborne)."""


def _development_weather_references(files: _InputFiles) -> tuple[str, ...]:
    tables = files.index().get("files")
    if not isinstance(tables, dict):
        raise InputError("Expected a files object in the prepared scenario index")
    if "development_weather" not in tables and "weather_catalog" not in tables:
        return tuple(f"../processed/weather/regional_wind_10m_{year}.csv" for year in (2023, 2024))
    references = tables.get("development_weather", [])
    if not isinstance(references, list) or any(
        not isinstance(path, str) or not path.strip() for path in references
    ):
        raise InputError("Expected files.development_weather to contain a list of CSV paths")
    if not references:
        raise InputError("B2 requires explicit UTC development weather files for this scenario package.")
    return tuple(references)


class WeatherReliability:
    """Empirical open-spell survival from designated development data."""

    def __init__(self, spells: dict[float, tuple[float, ...]],
                 source_sha256: dict[str, str] | None = None) -> None:
        self.spells = spells
        self.source_sha256 = dict(source_sha256 or {})

    @classmethod
    def from_development(cls, data_dir: Path, limits: set[float]) -> WeatherReliability:
        files = _InputFiles(data_dir)
        paths = _development_weather_references(files)
        files.digests.clear()  # The run input provenance already records the index digest.
        intervals: list[tuple[float, float, float]] = []
        for path in paths:
            previous_end = None
            for row in files.table(path):
                start = _utc(_field(row, "start_utc")).timestamp()
                end = _utc(_field(row, "end_utc")).timestamp()
                if end <= start or (previous_end is not None and start < previous_end):
                    raise InputError(f"Invalid or overlapping development weather interval in {path}")
                intervals.append((start, end, _number(row, "wind_speed_mps", minimum=0)))
                previous_end = end
        intervals.sort()
        if any(end > start for (_, end, _), (start, _, _) in zip(intervals, intervals[1:])):
            raise InputError("Development weather files contain overlapping UTC intervals")
        spells: dict[float, tuple[float, ...]] = {}
        for limit in sorted(limits):
            complete: list[float] = []
            opened_at: float | None = None
            previous_end: float | None = None
            known_start = False
            for start, end, wind in intervals:
                if previous_end != start:
                    opened_at = None
                    known_start = False
                if wind <= limit + WIND_TOLERANCE:
                    if opened_at is None:
                        opened_at = start
                else:
                    if opened_at is not None and known_start:
                        complete.append(start - opened_at)
                    known_start = True
                    opened_at = None
                previous_end = end
            spells[limit] = tuple(sorted(complete))
        return cls(spells, files.digests)

    def probability(self, limit: float, age_s: float, additional_s: float) -> float:
        durations = self.spells.get(limit, ())
        remaining = len(durations) - bisect_left(durations, age_s)
        if remaining < 5:
            # Current-wind persistence is the common fallback for sparse spells.
            return 1.0
        sufficient = len(durations) - bisect_left(durations, age_s + additional_s)
        return sufficient / remaining


def _overdue_area(last: float, start: float, end: float, interval: float) -> float:
    if end <= start:
        return 0.0
    return (max(0.0, end - last - interval)**2 - max(0.0, start - last - interval)**2) / (2 * interval)


class DispatchPolicy:
    MAX_PLANS = 12
    MAX_FIRST_STEPS = 6
    PLANNING_WINDOW_S = 7200

    def __init__(self, config: RunConfig, reliability: WeatherReliability | None = None) -> None:
        self.config = config
        self.reliability = reliability
        self.planning_comparison: dict[str, str | None] | None = None

    def choose(self, observation: Observation) -> str | None:
        candidates = sorted(observation.candidates, key=lambda candidate: self._priority(observation, candidate))
        self.planning_comparison = None
        if not candidates:
            return None
        if self.config.policy == "B0":
            return candidates[0].task_id
        if self.config.policy == "B1":
            return min(candidates, key=lambda candidate: self._score(observation, (candidate,), "coarse")).task_id
        first_steps = candidates[: self.MAX_FIRST_STEPS]
        plans: list[tuple[Candidate, ...]] = [(first,) for first in first_steps]
        for first in first_steps:
            followups = sorted(first.followups, key=lambda candidate: self._priority(observation, candidate))
            if followups:
                plans.append((first, followups[0]))
        spare = self.MAX_PLANS - len(plans)
        plans.extend((first,) for first in candidates[self.MAX_FIRST_STEPS : self.MAX_FIRST_STEPS + spare])
        self.planning_comparison = {
            mode: self._recommend(observation, plans, mode) for mode in ("full", "coarse")
        }
        return self.planning_comparison[self.config.planning_mode]

    def _recommend(self, observation: Observation, plans: list[tuple[Candidate, ...]],
                   mode: str) -> str | None:
        score, best = min(((self._score(observation, plan, mode), plan) for plan in plans),
                          key=lambda pair: pair[0])
        return best[0].task_id if score[0] < 0 else None

    @staticmethod
    def _priority(observation: Observation, candidate: Candidate) -> tuple:
        if observation.business == "batch":
            completion = observation.time_s + candidate.travel_s + candidate.service_s
            return completion > candidate.deadline_s, candidate.deadline_s, -candidate.priority_weight, candidate.task_id
        return -candidate.overdue_ratio, candidate.deadline_s, -candidate.priority_weight, candidate.task_id

    def _score(self, observation: Observation, plan: tuple[Candidate, ...], mode: str) -> tuple:
        now = observation.time_s
        completion = now
        benefit = 0.0
        probability = 1.0
        for candidate in plan:
            completion += candidate.travel_s + candidate.service_s
            if mode == "full" and self.reliability is not None:
                probability = min(probability, self.reliability.probability(
                    candidate.weather_limit_mps, candidate.open_age_s, completion - now,
                ))
            if observation.business == "batch":
                benefit += probability * (completion <= candidate.deadline_s)
            else:
                benefit += probability * self._periodic_gain(now, completion, candidate)
        last = plan[-1]
        turnaround = completion + last.return_s
        if mode == "full":
            turnaround += last.charging_wait_s
        return -benefit, turnaround, last.energy_wh, tuple(candidate.task_id for candidate in plan)

    def _periodic_gain(self, now: float, completion: float, candidate: Candidate) -> float:
        interval = candidate.stream_interval_s
        last = candidate.last_completion_s
        horizon = now + self.PLANNING_WINDOW_S
        if interval is None or last is None or completion >= horizon:
            return 0.0
        return _overdue_area(last, completion, horizon, interval) - _overdue_area(
            completion, completion, horizon, interval,
        )


def make_policy(config: RunConfig, scenario: Scenario, data_dir: Path) -> DispatchPolicy:
    reliability = None
    if config.policy == "B2":
        limits = {
            unit.max_service_wind_mps for template in scenario.templates.values() for unit in template
        }
        limits.update(min(profile.max_flight_wind_mps, profile.max_airspeed_mps)
                      for profile in scenario.profiles.values())
        reliability = WeatherReliability.from_development(data_dir, limits)
    return DispatchPolicy(config, reliability)
