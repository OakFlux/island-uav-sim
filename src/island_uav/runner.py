"""Portable configuration and observation adapter around the frozen study engine.

The execution, dispatch and independent audit modules are unchanged. Extra
position fields are observations for playback and never enter a policy.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from island_sim.audit import audit
from island_sim.domain import (
    AircraftProfile, BatchTask, FleetMember, PeriodicStream, RunConfig,
    Scenario, Station, UnitSpec, WeatherInterval,
)
from island_sim.engine import Simulation
from island_sim.inputs import _profile, _templates
from island_sim.ledger import EventLedger, read_events
from island_sim.policies import DispatchPolicy, WeatherReliability

DATA_DIR = Path(__file__).resolve().parent / "data"
SCHEMA = "island-uav-public-run-1"


def default_config() -> dict[str, Any]:
    """A short, active demonstration; use paper_case.json for the paper reference case."""
    return {
        "scenario": "pingtan", "fleet_size": 4, "targets_per_sortie": 2,
        "policy": "B2", "charge_ports": 2, "port_input_w": 240,
        "duration_hours": 6, "warmup_hours": 0, "recovery_hours": 1,
        "weather": "synthetic", "historical_start_hour": 24,
        "weather_arrangement": "variable", "task_seed": 101,
        "initial_age_mode": "due", "execution_mode": "full",
    }


def load_config(config: str | Path | Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Read a JSON object, reject unknown keys, and check finite limits."""
    value = default_config()
    if config is None:
        incoming: dict = {}
    elif isinstance(config, Mapping):
        incoming = dict(config)
    else:
        incoming = json.loads(Path(config).read_text(encoding="utf-8"))
    if not isinstance(incoming, dict):
        raise ValueError("Configuration must be a JSON object.")
    unknown = set(incoming) - set(value)
    if unknown:
        raise ValueError("Unknown configuration keys: " + ", ".join(sorted(unknown)))
    value.update(incoming)
    choices = {
        "scenario": {"pingtan"}, "fleet_size": {2, 4, 8},
        "targets_per_sortie": {1, 2}, "policy": {"B0", "B2"},
        "charge_ports": {1, 2}, "weather": {"synthetic", "historical"},
        "task_seed": {101, 202, 303}, "initial_age_mode": {"due", "paper"},
        "weather_arrangement": {"variable", "continuous", "fragmented"},
        "execution_mode": {"full", "instant_charge", "accumulating"},
    }
    integer_choices = {"fleet_size", "targets_per_sortie", "charge_ports", "task_seed"}
    for key, allowed in choices.items():
        item = value[key]
        valid_type = type(item) is int if key in integer_choices else isinstance(item, str)
        if not valid_type or item not in allowed:
            raise ValueError(f"{key} must be one of {sorted(allowed)}")
    for key, low, high in (
        ("port_input_w", 1, 2000), ("duration_hours", 0.05, 72),
        ("warmup_hours", 0, 24), ("recovery_hours", 0.05, 2),
        ("historical_start_hour", 0, 96),
    ):
        item = value[key]
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item):
            raise ValueError(f"{key} must be a finite number.")
        if not low <= item <= high:
            raise ValueError(f"{key} must be between {low} and {high}.")
    if value["weather"] == "historical":
        hours = sum(value[key] for key in (
            "historical_start_hour", "warmup_hours", "duration_hours", "recovery_hours"))
        if hours > 97:
            raise ValueError("Historical selection exceeds the bundled 97-hour wind block.")
    return value


def _rows(name: str) -> list[dict[str, str]]:
    with (DATA_DIR / name).open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def _json(name: str) -> dict:
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


def _digest() -> str:
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(DATA_DIR.iterdir()) if p.suffix in {".csv", ".json"}}
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def _weather(config: dict, end: float) -> tuple[WeatherInterval, ...]:
    intervals = []
    if config["weather"] == "historical":
        origin = datetime.fromisoformat("2025-04-04T00:00:00+00:00") + timedelta(
            hours=config["historical_start_hour"])
        for row in _rows("historical_wind.csv"):
            start = (datetime.fromisoformat(row["start_utc"].replace("Z", "+00:00")) - origin).total_seconds()
            finish = (datetime.fromisoformat(row["end_utc"].replace("Z", "+00:00")) - origin).total_seconds()
            visible = (datetime.fromisoformat(row["visible_at_utc"].replace("Z", "+00:00")) - origin).total_seconds()
            if finish > 0 and start < end:
                intervals.append(WeatherInterval(max(0.0, start), min(end, finish), max(0.0, visible),
                                                  float(row["wind_east_mps"]), float(row["wind_north_mps"])))
    else:
        arrangements = {
            "continuous": [(360.0, 5.0), (360.0, 11.0)],
            "fragmented": [(120.0, 5.0), (120.0, 11.0)],
            "variable": [(1200.0, 5.0), (600.0, 9.5), (900.0, 6.0),
                         (300.0, 11.0), (600.0, 7.5)],
        }
        pattern = arrangements[config["weather_arrangement"]]
        cursor = 0.0
        while cursor < end:
            for duration, speed in pattern:
                finish = min(end, cursor + duration)
                intervals.append(WeatherInterval(cursor, finish, cursor, speed * 0.6, -speed * 0.8))
                cursor = finish
                if cursor >= end:
                    break
    if not intervals or intervals[0].start_s != 0 or intervals[-1].end_s < end:
        raise ValueError("Weather input does not cover the simulation horizon.")
    return tuple(intervals)


def build_scenario(config: Mapping[str, Any] | str | Path | None = None) -> Scenario:
    config = load_config(config)
    assets = _rows("assets.csv")
    base = _rows("bases.csv")[0]
    positions = {row["asset_id"]: (float(row["x_m"]), float(row["y_m"])) for row in assets}
    positions[base["base_id"]] = (float(base["x_m"]), float(base["y_m"]))
    legs = frozenset(tuple(sorted((r["node_a"], r["node_b"]))) for r in _rows("legs.csv")
                     if r["allowed_for_task_level_model"].lower() == "true")
    profiles = {r["profile_id"]: _profile(r) for r in _rows("profiles.csv")}
    templates = _templates(_rows("templates.csv"))
    fleet = tuple(FleetMember(f"U{i+1:02d}", "LGT" if i % 2 == 0 else "GEN", 1.0)
                  for i in range(config["fleet_size"]))
    station = Station("PUBLIC", 8, 1, config["charge_ports"], float(config["port_input_w"]),
                      0.9, 1.0, 30.0, 30.0, 60.0, 240.0)
    streams = []
    for row in _rows(f"demand_{config['task_seed']}.csv"):
        interval = float(row["target_interval_s"])
        initial = float(row["initial_age_s"]) if config["initial_age_mode"] == "paper" else interval
        streams.append(PeriodicStream(row["task_stream_id"], row["asset_id"], row["template_id"],
                                      interval, initial, float(row["priority_weight"])))
    warmup = config["warmup_hours"] * 3600.0
    end = warmup + config["duration_hours"] * 3600.0
    recovery = end + config["recovery_hours"] * 3600.0
    scenario_id = (f"pingtan-{config['weather']}-F{config['fleet_size']}-K{config['targets_per_sortie']}"
                   f"-C{config['charge_ports']}-{config['policy']}")
    return Scenario(
        scenario_id=scenario_id, business="periodic", base_id=base["base_id"],
        positions=positions, allowed_legs=legs, profiles=profiles, templates=templates,
        fleet=fleet, station=station, weather=_weather(config, recovery), batch_tasks=(),
        periodic_streams=tuple(streams), evaluation_start_s=warmup,
        evaluation_end_s=end, recovery_end_s=recovery,
        provenance={"inputs_sha256": _digest(), "coordinate_system": "EPSG:32650",
                    "weather_role": "historical regional proxy" if config["weather"] == "historical"
                    else "controlled synthetic illustration", "initial_age_mode": config["initial_age_mode"]},
    )


def scenario_metadata(scenario: Scenario) -> dict:
    """Geographic display metadata; physical travel uses projected meters."""
    sites = []
    demand = {stream.asset_id: stream for stream in scenario.periodic_streams}
    for row in _rows("assets.csv"):
        stream = demand[row["asset_id"]]
        sites.append({
            "id": row["asset_id"], "asset_id": row["asset_id"],
            "lon": float(row["longitude"]), "lat": float(row["latitude"]),
            "x_m": float(row["x_m"]), "y_m": float(row["y_m"]),
            "evidence": row["location_evidence"], "template_id": stream.template_id,
            "target_interval_s": stream.target_interval_s, "initial_age_s": stream.initial_age_s,
        })
    base = _rows("bases.csv")[0]
    return {"sites": sites, "base": {"id": scenario.base_id, "lon": float(base["longitude"]),
                                       "lat": float(base["latitude"]), "x_m": float(base["x_m"]),
                                       "y_m": float(base["y_m"])},
            "crs": "EPSG:32650", "historical_origin_utc": "2025-04-04T00:00:00Z",
            "position_observation": "position_xy is the aircraft position at event time; energy_interval positions are at end_s"}


class _ObservedLedger(EventLedger):
    """Extra output-only telemetry, added without changing execution events."""
    simulation: Simulation | None = None

    def emit(self, time_s: float, kind: str, **fields: Any) -> None:
        aircraft_id = fields.get("aircraft_id")
        if self.simulation is not None and aircraft_id in self.simulation.aircraft:
            aircraft = self.simulation.aircraft[aircraft_id]
            fields.update(position_xy=list(aircraft.position), node=aircraft.node,
                          destination=aircraft.destination, phase_state=aircraft.phase)
        super().emit(time_s, kind, **fields)


def _serialize_scenario(scenario: Scenario) -> dict:
    value = asdict(scenario)
    value["allowed_legs"] = sorted([list(leg) for leg in scenario.allowed_legs])
    for profile in value["profiles"].values():
        profile["capabilities"] = sorted(profile["capabilities"])
    return value


def _restore_scenario(value: dict) -> Scenario:
    value = dict(value)
    value["positions"] = {k: tuple(v) for k, v in value["positions"].items()}
    value["allowed_legs"] = frozenset(tuple(leg) for leg in value["allowed_legs"])
    value["profiles"] = {k: AircraftProfile(**{**p, "capabilities": frozenset(p["capabilities"])})
                          for k, p in value["profiles"].items()}
    value["templates"] = {k: tuple(UnitSpec(**unit) for unit in units) for k, units in value["templates"].items()}
    value["fleet"] = tuple(FleetMember(**member) for member in value["fleet"])
    value["station"] = Station(**value["station"])
    value["weather"] = tuple(WeatherInterval(**interval) for interval in value["weather"])
    value["batch_tasks"] = tuple(BatchTask(**task) for task in value["batch_tasks"])
    value["periodic_streams"] = tuple(PeriodicStream(**stream) for stream in value["periodic_streams"])
    return Scenario(**value)


def run_simulation(config: Mapping[str, Any] | str | Path | None = None,
                   output: str | Path | None = None) -> dict[str, Any]:
    """Execute once, independently audit, and optionally save a new run folder."""
    config = load_config(config)
    scenario = build_scenario(config)
    run_config = RunConfig(execution_mode=config["execution_mode"], policy=config["policy"],
                           planning_mode="full", periodic_release_fraction=0.5,
                           max_targets_per_sortie=config["targets_per_sortie"])
    source = _json("development_spells.json")
    reliability = None
    if config["policy"] == "B2":
        reliability = WeatherReliability({float(k): tuple(v) for k, v in source["spells_s"].items()},
                                          source["source_sha256"])
    destination = io.StringIO()
    ledger = _ObservedLedger(destination)
    simulation = Simulation(scenario, run_config, DispatchPolicy(run_config, reliability), ledger)
    ledger.simulation = simulation
    simulation.run()
    events = list(read_events(io.StringIO(destination.getvalue())))
    report = audit(scenario, events, run_config.execution_mode,
                   periodic_release_fraction=run_config.periodic_release_fraction,
                   max_targets_per_sortie=run_config.max_targets_per_sortie)
    manifest = {"schema": SCHEMA, "scenario_id": scenario.scenario_id,
                "config": config, "execution_config": asdict(run_config),
                "inputs_sha256": scenario.provenance["inputs_sha256"],
                "engine_sha256": hashlib.sha256(Path(__import__("island_sim.engine", fromlist=["__file__"]).__file__).read_bytes()).hexdigest(),
                "development_weather_sha256": source["source_sha256"],
                "telemetry": "output-only position fields; frozen engine and policy source unchanged"}
    metadata = scenario_metadata(scenario)
    result = {"scenario": scenario, "config": config, "events": events,
              "audit": report, "manifest": manifest, "metadata": metadata}
    if output is not None:
        folder = Path(output)
        folder.mkdir(parents=True, exist_ok=False)
        files = {"manifest.json": manifest, "audit.json": report,
                 "scenario.json": _serialize_scenario(scenario), "config.json": config,
                 "metadata.json": metadata, "status.json": {"status": report["status"]}}
        for name, payload in files.items():
            (folder / name).write_text(json.dumps(payload, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        (folder / "events.jsonl").write_text(destination.getvalue(), encoding="utf-8")
    return result


def audit_saved_run(folder: str | Path) -> dict:
    """Recalculate metrics from the saved scenario and ledger, without new runs."""
    folder = Path(folder)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != SCHEMA:
        raise ValueError("Unknown run-manifest schema.")
    scenario = _restore_scenario(json.loads((folder / "scenario.json").read_text(encoding="utf-8")))
    with (folder / "events.jsonl").open(encoding="utf-8") as stream:
        return audit(scenario, read_events(stream), **{
            key: manifest["execution_config"][key]
            for key in ("execution_mode", "periodic_release_fraction", "max_targets_per_sortie")})
