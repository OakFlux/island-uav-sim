"""Portable assembly, execution and independent accounting of prepared island cases.

The public adapter changes inputs, never the physical event executor. The model
uses metres/seconds/watts/watt-hours; browser coordinates are display metadata.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from island_sim_v5 import simulate
from .core.delivery import evaluate_delivery
from .core.delivery_audit import audit_delivery
from .core.independent_audit import audit_result
from .core.wind_frames import rotate_weather_to_grid

DATA = Path(__file__).resolve().parent / "data"
SCHEMA = "island-uav-public-run-2"
NETWORKS = ("legacy_24", "pingtan_48", "pingtan_96", "pingtan_192")


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def default_config() -> dict:
    """An active six-hour demonstration, not a formal paper experiment."""
    return dict(network_id="pingtan_96", fleet_size=12, targets_per_sortie=2,
                charge_ports=1, port_input_w=240, policy="B2", recovery_policy="NEAR",
                weather="synthetic", duration_hours=6, warmup_hours=0,
                recovery_hours=1, task_seed=101, historical_block="2025-04-04",
                historical_start_hour=0, weather_arrangement="variable",
                initial_age_mode="due", execution_mode="full")


def load_config(config: dict | str | Path | None = None) -> dict:
    """Load and validate the single public configuration schema.

    Unknown keys and booleans used as numbers are rejected. Historical input is
    restricted to the bundled 97-hour block; no network download occurs.
    """
    if config is None:
        supplied = {}
    elif isinstance(config, (str, Path)):
        supplied = _read(Path(config))
    elif isinstance(config, dict):
        supplied = copy.deepcopy(config)
    else:
        raise ValueError("Configuration must be a JSON object or its file path")
    if not isinstance(supplied, dict):
        raise ValueError("Configuration must be a JSON object")
    cfg = default_config()
    unknown = set(supplied) - set(cfg)
    if unknown:
        raise ValueError("Unknown configuration keys: " + ", ".join(sorted(unknown)))
    cfg.update(supplied)
    choices = {"network_id": NETWORKS, "policy": ("B0", "B2"),
               "recovery_policy": ("HOME", "NEAR", "QUEUE"),
               "weather": ("synthetic", "historical"),
               "weather_arrangement": ("variable", "continuous", "fragmented"),
               "initial_age_mode": ("due", "paper"),
               "execution_mode": ("full", "instant_charge", "accumulating")}
    for key, allowed in choices.items():
        if cfg[key] not in allowed:
            raise ValueError(f"{key} must be one of {', '.join(allowed)}")
    for key in ("fleet_size", "targets_per_sortie", "charge_ports", "task_seed", "historical_start_hour"):
        if type(cfg[key]) is not int:
            raise ValueError(f"{key} must be an integer")
    allowed_fleet = (2, 4, 8) if cfg["network_id"] == "legacy_24" else (6, 12, 24)
    if cfg["fleet_size"] not in allowed_fleet:
        raise ValueError(f"fleet_size for {cfg['network_id']} must be {allowed_fleet}")
    if cfg["targets_per_sortie"] not in (1, 2):
        raise ValueError("targets_per_sortie must be 1 or 2")
    if cfg["charge_ports"] not in (1, 2):
        raise ValueError("charge_ports must be 1 or 2 per base")
    if cfg["task_seed"] not in (101, 202, 303):
        raise ValueError("task_seed must be 101, 202 or 303")
    ranges = {"port_input_w": (1, 2000), "duration_hours": (1 / 60, 72),
              "warmup_hours": (0, 24), "recovery_hours": (0, 4)}
    for key, (low, high) in ranges.items():
        value = cfg[key]
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{key} must be a finite number between {low:g} and {high:g}")
    if not 0 <= cfg["historical_start_hour"] < 97:
        raise ValueError("historical_start_hour must be between 0 and 96")
    if cfg["historical_block"] != "2025-04-04":
        raise ValueError("The bundled historical block starts on 2025-04-04")
    if cfg["weather"] == "historical":
        stop = sum(cfg[k] for k in ("warmup_hours", "duration_hours", "recovery_hours"))
        if cfg["historical_start_hour"] + stop > 97 + 1e-9:
            raise ValueError("Historical simulation exceeds the bundled 97-hour block")
        if cfg["weather_arrangement"] != "variable":
            raise ValueError("Historical weather must retain weather_arrangement='variable'")
    return cfg


validate_config = load_config


def _synthetic_weather(zones: list[dict], stop_s: float, arrangement: str) -> dict:
    """Deterministic illustrative grid-axis winds, independent of evaluation data."""
    result = {}
    # Twenty-minute intervals expose spatially different task opening/closure.
    step_s = 1200.0
    for i, zone in enumerate(zones):
        rows = []
        left = 0.0
        while left < stop_s:
            right = min(left + step_s, stop_s)
            midpoint = (left + right) / 2
            if arrangement == "variable":
                speed = 6.0 + 3.0 * math.sin(2 * math.pi * midpoint / 10800 + i * .65)
            else:
                open_s = 360.0 if arrangement == "continuous" else 120.0
                # Edges must follow opening periods exactly for controlled demonstrations.
                right = min(left + open_s, stop_s)
                speed = 5.0 if int(left / open_s + i) % 2 == 0 else 11.0
            angle = 2 * math.pi * midpoint / 21600 + i * .22
            rows.append(dict(start_s=left, end_s=right,
                             east_mps=speed * math.cos(angle), north_mps=speed * math.sin(angle),
                             vector_frame="projected_grid_xy"))
            left = right
        result[zone["id"]] = rows
    return result


def _historical_weather(cfg: dict, scene: dict) -> dict:
    product = "era5" if cfg["network_id"] == "legacy_24" else "ifs"
    block = _read(DATA / f"weather/{product}_20250404.json")
    epoch = datetime(2025, 4, 4, tzinfo=timezone.utc)
    start = epoch + timedelta(hours=cfg["historical_start_hour"])
    stop_s = scene["clock"]["recovery_end_s"]
    grouped = {}
    for row in block["rows"]:
        t = datetime.fromisoformat(row["time_utc"].replace("Z", "+00:00"))
        left, right = (t-start).total_seconds(), (t-start).total_seconds()+3600
        if right <= 0 or left >= stop_s:
            continue
        grouped.setdefault(row["zone_id"], []).append(dict(start_s=max(0., left), end_s=min(stop_s, right),
                                                          east_mps=row["east_mps"], north_mps=row["north_mps"]))
    return rotate_weather_to_grid(grouped, scene["weather_zones"],
                                  _read(DATA / "weather/wind_coordinate_frames.json"), cfg["network_id"])


def _engine_config(cfg: dict) -> dict:
    engine = dict(policy=cfg["policy"], recovery=cfg["recovery_policy"],
                  max_targets_per_sortie=cfg["targets_per_sortie"],
                  execution_mode=cfg["execution_mode"], record_events=True,
                  air_capacity=8 if cfg["network_id"] == "legacy_24" else 24)
    if cfg["policy"] == "B2":
        product = "era5" if cfg["network_id"] == "legacy_24" else "ifs"
        engine["reliability"] = _read(DATA / f"weather/{product}_development_spells.json")
    return engine


def build_scenario(config: dict | str | Path | None = None) -> dict:
    cfg = load_config(config)
    scene = _read(DATA / f"networks/{cfg['network_id']}_seed{cfg['task_seed']}.json")
    per_base = cfg["fleet_size"] // len(scene["bases"])
    scene["fleet"] = [member for base in scene["bases"]
                      for member in [m for m in scene["fleet"] if m["home_base_id"] == base["id"]][:per_base]]
    if len(scene["fleet"]) != cfg["fleet_size"]:
        raise ValueError("Insufficient prepared aircraft capacity")
    for base in scene["bases"]:
        base.update(charge_ports=cfg["charge_ports"], port_input_w=cfg["port_input_w"])
    if cfg["initial_age_mode"] == "due":
        for task in scene["tasks"]:
            task["initial_age_s"] = task["interval_s"]
    warmup = cfg["warmup_hours"] * 3600
    end = warmup + cfg["duration_hours"] * 3600
    scene["clock"] = dict(evaluation_start_s=warmup, evaluation_end_s=end,
                          recovery_end_s=end + cfg["recovery_hours"] * 3600)
    scene["business"] = "periodic"
    scene["weather"] = (_historical_weather(cfg, scene) if cfg["weather"] == "historical"
                        else _synthetic_weather(scene["weather_zones"], scene["clock"]["recovery_end_s"], cfg["weather_arrangement"]))
    scene["scenario_id"] = f"public-{cfg['network_id']}-s{cfg['task_seed']}"
    scene.setdefault("metadata", {}).update(public_demo=True,
        weather_label=("Illustrative synthetic wind; not a paper experiment" if cfg["weather"] == "synthetic"
                       else "Historical 2025 wind, user-selected configuration"),
        wind_execution_reference_frame="EPSG:32650 projected grid easting/northing")
    return scene


def _metadata(scene: dict, cfg: dict, reference: str | None = None) -> dict:
    display = _read(DATA / "display_coordinates.json")[cfg["network_id"]]
    def located(position_id: str, value: dict) -> dict:
        return {**value, **scene["positions"][position_id], **display[position_id]}
    sites = [located(task["position_id"], task) for task in scene["tasks"]]
    bases = [located(base["position_id"], base) for base in scene["bases"]]
    zones = []
    frames = _read(DATA / "weather/wind_coordinate_frames.json")
    group = "legacy_24" if cfg["network_id"] == "legacy_24" else "pingtan"
    for zone in scene["weather_zones"]:
        z = dict(zone)
        if "longitude" not in z:
            f = frames["networks"][group]["zones"][z["id"]]
            z.update(longitude=f["scene_anchor_longitude"], latitude=f["scene_anchor_latitude"])
        zones.append(z)
    return dict(schema_version=2, crs="EPSG:32650", sites=sites, bases=bases,
                weather_zones=zones, profiles=copy.deepcopy(scene["profiles"]),
                weather_source=cfg["weather"],
                weather_label=("Illustrative synthetic wind — demonstration only" if cfg["weather"] == "synthetic"
                               else "Historical ERA5-derived wind" if group == "legacy_24" else "Historical ECMWF IFS regional wind"),
                is_formal_reference=reference is not None, reference_case=reference,
                run_purpose="Selected archived reference reproduction" if reference else "User-configured demonstration",
                historical_start_utc=(datetime(2025,4,4,tzinfo=timezone.utc)+timedelta(hours=cfg["historical_start_hour"])).isoformat()
                    if cfg["weather"] == "historical" else None,
                map_attribution="© OpenStreetMap contributors", coordinates_for_display_only=True)


def _execute(scene: dict, engine_config: dict, cfg: dict, output: str | Path | None,
             reference: str | None = None) -> dict:
    folder = Path(output) if output is not None else None
    if folder is not None and (folder / "manifest.json").exists():
        raise FileExistsError(f"Refusing to overwrite an existing simulation: {folder}")
    start = time.perf_counter()
    physical = simulate(scene, engine_config)
    physical_audit = audit_result(scene, physical)
    delivery = evaluate_delivery(scene, physical, mode="base")
    delivery_audit = audit_delivery(scene, physical, delivery)
    status = "pass" if physical_audit["status"] == delivery_audit["status"] == "pass" else "fail"
    audit = dict(status=status, physical=physical_audit, delivery=delivery_audit)
    metadata = _metadata(scene, cfg, reference)
    manifest = dict(schema=SCHEMA, package_version="0.2.0", elapsed_s=time.perf_counter()-start,
                    reference_case=reference, scope="One simulation; selected reference cases do not reproduce the entire paper study.",
                    physical_core="Frozen V5 executor, unchanged", recorded_safety_flags=physical["metrics"]["safety_violations"],
                    audit_interpretation="Independent service, energy, resource and delivery accounting consistency; not a safety certification.")
    result = dict(scenario=scene, physical=physical, delivery=delivery, config=cfg,
                  metadata=metadata, audit=audit, manifest=manifest, events=physical["events"])
    if folder is not None:
        folder.mkdir(parents=True, exist_ok=True)
        for name, value in (("scenario",scene),("physical",physical),("delivery",delivery),
                            ("config",cfg),("metadata",metadata),("audit",audit),("manifest",manifest)):
            _write(folder / f"{name}.json", value)
        _write(folder / "execution_config.json", engine_config)
        with (folder / "events.jsonl").open("w", encoding="utf-8") as f:
            for event in physical["events"]:
                f.write(json.dumps(event, ensure_ascii=False, allow_nan=False, separators=(",", ":"))+"\n")
        manifest["sha256"] = {p.name:_hash(p) for p in folder.iterdir() if p.name in
                               ("scenario.json","physical.json","delivery.json","config.json","metadata.json","execution_config.json","events.jsonl")}
        _write(folder / "manifest.json", manifest)
    return result


def run_simulation(config: dict | str | Path | None = None, output: str | Path | None = None) -> dict:
    cfg = load_config(config)
    return _execute(build_scenario(cfg), _engine_config(cfg), cfg, output)


def audit_saved_run(folder: str | Path) -> dict:
    """Recompute independent accounts and check saved source-file integrity."""
    directory = Path(folder)
    scene = _read(directory / "scenario.json")
    physical = _read(directory / "physical.json")
    delivery = _read(directory / "delivery.json")
    physical_audit = audit_result(scene, physical)
    delivery_audit = audit_delivery(scene, physical, delivery)
    manifest = _read(directory / "manifest.json")
    integrity = [{"file":name, "match":(directory/name).is_file() and _hash(directory/name)==digest}
                 for name,digest in manifest.get("sha256",{}).items()]
    status = "pass" if physical_audit["status"] == delivery_audit["status"] == "pass" and all(r["match"] for r in integrity) else "fail"
    return dict(status=status, physical=physical_audit, delivery=delivery_audit, integrity=integrity)
