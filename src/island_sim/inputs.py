"""Load the prepared island-study tables without accessing raw sources or the network."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping

from .domain import (
    AircraftProfile,
    BatchTask,
    FleetMember,
    PeriodicStream,
    Scenario,
    Station,
    UnitSpec,
    WeatherInterval,
)

INPUT_SCHEMA = "island-inspection-inputs-1.0"
DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data"


class InputError(ValueError):
    """A study selector or prepared data file does not satisfy the input schema."""


class _InputFiles:
    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir.resolve()
        self.scenarios = self.root / "scenarios"
        self.digests: dict[str, str] = {}

    def path(self, relative: str) -> Path:
        path = (self.scenarios / relative).resolve()
        if not path.is_relative_to(self.root):
            raise InputError(f"Input path leaves the prepared data directory: {relative}")
        return path

    def read(self, relative: str) -> bytes:
        path = self.path(relative)
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise InputError(f"Cannot read prepared input {relative}: {exc}") from exc
        self.digests[path.relative_to(self.root).as_posix()] = hashlib.sha256(payload).hexdigest()
        return payload

    def table(self, relative: str) -> list[dict[str, str]]:
        try:
            reader = csv.DictReader(io.StringIO(self.read(relative).decode("utf-8-sig"), newline=""))
            fields = reader.fieldnames
            if not fields or len(fields) != len(set(fields)):
                raise InputError(f"Empty or duplicate CSV columns in {relative}")
            rows = list(reader)
            if not rows or any(None in row or None in row.values() for row in rows):
                raise InputError(f"Empty or malformed prepared table: {relative}")
            return rows
        except (UnicodeError, csv.Error) as exc:
            raise InputError(f"Malformed prepared table {relative}: {exc}") from exc

    def index(self) -> dict:
        try:
            index = json.loads(self.read("scenario_index.json"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise InputError(f"Malformed scenario index: {exc}") from exc
        if not isinstance(index, dict) or index.get("schema_version") != INPUT_SCHEMA:
            raise InputError(f"Expected scenario index schema {INPUT_SCHEMA}")
        return index

    def combined_digest(self) -> str:
        entries = json.dumps(self.digests, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(entries.encode()).hexdigest()


def _field(row: Mapping, name: str) -> str:
    value = row.get(name)
    if not isinstance(value, str) or not value.strip():
        raise InputError(f"Missing value for {name}: {row}")
    return value.strip()


def _number(row: Mapping, name: str, *, minimum: float | None = None,
            maximum: float | None = None) -> float:
    raw = _field(row, name)
    try:
        value = float(raw)
    except ValueError as exc:
        raise InputError(f"{name} is not numeric: {raw!r}") from exc
    if not math.isfinite(value):
        raise InputError(f"{name} must be finite: {raw!r}")
    if minimum is not None and value < minimum:
        raise InputError(f"{name} must be at least {minimum}: {raw!r}")
    if maximum is not None and value > maximum:
        raise InputError(f"{name} must be at most {maximum}: {raw!r}")
    return value


def _positive(row: Mapping, name: str) -> float:
    value = _number(row, name)
    if value <= 0:
        raise InputError(f"{name} must be positive: {value}")
    return value


def _integer(row: Mapping, name: str, *, minimum: int = 1) -> int:
    value = _number(row, name, minimum=minimum)
    if not value.is_integer():
        raise InputError(f"{name} must be an integer: {value}")
    return int(value)


def _unique(rows: Iterable[Mapping], key: str) -> dict[str, Mapping]:
    result = {}
    for row in rows:
        value = _field(row, key)
        if value in result:
            raise InputError(f"Duplicate {key}: {value}")
        result[value] = row
    return result


def _select(options: Mapping, key: str, description: str):
    if key not in options:
        available = ", ".join(str(item) for item in sorted(options))
        raise InputError(f"Unknown {description} {key!r}. Available: {available}")
    return options[key]


def _profile(row: Mapping) -> AircraftProfile:
    capabilities = frozenset(_field(row, "capabilities").split("|"))
    if not all(capabilities) or any(item.strip() != item for item in capabilities):
        raise InputError("Aircraft capabilities must be nonempty pipe-separated identifiers")
    profile = AircraftProfile(
        profile_id=_field(row, "profile_id"),
        capabilities=capabilities,
        battery_usable_wh=_positive(row, "battery_usable_wh"),
        return_reserve_wh=_number(row, "return_reserve_wh", minimum=0),
        max_airspeed_mps=_positive(row, "max_airspeed_mps"),
        max_groundspeed_mps=_positive(row, "max_groundspeed_mps"),
        min_groundspeed_mps=_positive(row, "min_groundspeed_mps"),
        max_flight_wind_mps=_positive(row, "max_flight_wind_mps"),
        takeoff_w=_positive(row, "takeoff_w"),
        cruise_w=_positive(row, "cruise_w"),
        inspection_base_w=_positive(row, "inspection_base_w"),
        hold_w=_positive(row, "hold_w"),
        landing_w=_positive(row, "landing_w"),
        ground_w=_number(row, "ground_w", minimum=0),
        charge_intake_battery_w=_positive(row, "charge_intake_battery_w"),
    )
    if profile.return_reserve_wh >= profile.battery_usable_wh:
        raise InputError(f"Return reserve must be smaller than battery capacity: {profile.profile_id}")
    if profile.min_groundspeed_mps > profile.max_groundspeed_mps:
        raise InputError(f"Reversed ground speed limits: {profile.profile_id}")
    return profile


def _unit(row: Mapping) -> UnitSpec:
    return UnitSpec(
        order=_integer(row, "unit_order"),
        required_capability=_field(row, "required_capability"),
        duration_s=_positive(row, "duration_s"),
        max_service_wind_mps=_number(row, "max_service_wind_mps", minimum=0),
        inspection_power_factor=_positive(row, "inspection_power_factor"),
    )


def _templates(rows: Iterable[Mapping]) -> dict[str, tuple[UnitSpec, ...]]:
    units: dict[str, list[UnitSpec]] = {}
    for row in rows:
        units.setdefault(_field(row, "template_id"), []).append(_unit(row))
    result = {}
    for name, template in units.items():
        ordered = tuple(sorted(template, key=lambda unit: unit.order))
        if tuple(unit.order for unit in ordered) != tuple(range(1, len(ordered) + 1)):
            raise InputError(f"Inspection orders must be consecutive from 1: {name}")
        result[name] = ordered
    return result


def _station(row: Mapping) -> Station:
    station = Station(
        station_setting_id=_field(row, "station_setting_id"),
        air_capacity=_integer(row, "air_capacity"),
        pad_capacity=_integer(row, "pad_capacity"),
        charge_ports=_integer(row, "charge_ports"),
        port_input_w=_positive(row, "port_input_w"),
        charge_efficiency=_number(row, "charge_efficiency", minimum=0, maximum=1),
        full_charge_soc_fraction=_number(row, "full_charge_soc_fraction", minimum=0, maximum=1),
        takeoff_s=_positive(row, "takeoff_s"),
        landing_s=_positive(row, "landing_s"),
        turnaround_s=_number(row, "turnaround_s", minimum=0),
        maximum_landing_hold_s=_number(row, "maximum_landing_hold_s", minimum=0),
    )
    if station.charge_efficiency == 0 or station.full_charge_soc_fraction == 0:
        raise InputError("Charging efficiency and target SOC must be positive")
    return station


def _geometry(files: _InputFiles, layout: Mapping, bases: Iterable[Mapping]):
    layout_id = _field(layout, "layout_id")
    base = _select(_unique(bases, "layout_id"), layout_id, "base layout")
    coordinate_system = _field(layout, "coordinate_system")
    if coordinate_system != _field(base, "coordinate_system"):
        raise InputError(f"Base and asset coordinate systems differ for {layout_id}")
    base_id = _field(base, "base_id")
    assets = _unique(files.table(_field(layout, "assets")), "asset_id")
    if base_id in assets or len(assets) != layout["asset_count"]:
        raise InputError(f"Duplicate base/asset ID or incorrect asset count for {layout_id}")
    positions = {
        identifier: (_number(row, "x_m"), _number(row, "y_m"))
        for identifier, row in {base_id: base, **assets}.items()
    }
    known = set()
    allowed = set()
    for row in files.table(_field(layout, "undirected_legs")):
        a, b = _field(row, "node_a"), _field(row, "node_b")
        pair = tuple(sorted((a, b)))
        if a not in positions or b not in positions or a == b or pair in known:
            raise InputError(f"Unknown, self or duplicate leg: {pair}")
        known.add(pair)
        length = _positive(row, "distance_m")
        if not math.isclose(length, math.dist(positions[a], positions[b]), abs_tol=0.01):
            raise InputError(f"Leg geometry does not match endpoint coordinates: {pair}")
        # The fully synthetic layout has no OSM mask and leaves this field blank.
        outside = (0.0 if coordinate_system == "local Cartesian m" and row.get("outside_osm_mask_m") == ""
                   else _number(row, "outside_osm_mask_m", minimum=0))
        flag = _field(row, "allowed_for_task_level_model").lower()
        if flag not in {"true", "false"}:
            raise InputError(f"Invalid leg availability: {pair}")
        if flag == "true":
            if outside > 0.01:
                raise InputError(f"Allowed leg crosses the prepared island mask: {pair}")
            allowed.add(pair)
    if not allowed:
        raise InputError(f"No allowed legs for {layout_id}")
    return base_id, positions, frozenset(allowed), frozenset(assets)


def _utc(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InputError(f"Invalid UTC timestamp: {value}") from exc
    if result.utcoffset() != timedelta(0):
        raise InputError(f"Expected an explicit UTC timestamp: {value}")
    return result.astimezone(timezone.utc)


def _weather(files: _InputFiles, relative: str, origin: datetime | None,
             end_s: float, source_id: str | None = None, *,
             ordered_publications: bool = False) -> tuple[WeatherInterval, ...]:
    intervals = []
    previous_end = -math.inf
    previous_visible = -math.inf
    source = source_id
    for row in files.table(relative):
        if origin is None:
            start = _number(row, "start_s", minimum=0)
            end = _number(row, "end_s")
            visible = _number(row, "visible_at_s")
        else:
            start = (_utc(_field(row, "start_utc")) - origin).total_seconds()
            end = (_utc(_field(row, "end_utc")) - origin).total_seconds()
            visible = (_utc(_field(row, "visible_at_utc")) - origin).total_seconds()
        if start < previous_end or end <= start or visible < start:
            raise InputError(f"Invalid native weather interval or early visibility in {relative}: {row}")
        if ordered_publications and visible < previous_visible:
            raise InputError(f"Weather publication times must be nondecreasing in {relative}: {row}")
        previous_end = end
        previous_visible = visible
        row_source = _field(row, "weather_source_id")
        source = source or row_source
        if row_source != source:
            raise InputError(f"Weather source identity changes in {relative}: {row_source}")
        if end <= 0 or start >= end_s:
            continue
        east = _number(row, "wind_east_mps")
        north = _number(row, "wind_north_mps")
        speed = _number(row, "wind_speed_mps", minimum=0)
        if not math.isclose(math.hypot(east, north), speed, rel_tol=1e-6, abs_tol=1e-6):
            raise InputError(f"Wind magnitude differs from component norm in {relative}: {row}")
        left, right = max(0.0, start), min(end_s, end)
        cursor = intervals[-1].end_s if intervals else 0.0
        if left != cursor:
            raise InputError(f"Weather does not cover selected horizon at {cursor:g}s: {relative}")
        intervals.append(WeatherInterval(left, right, max(0.0, visible), east, north))
    if not intervals or intervals[-1].end_s != end_s:
        raise InputError(f"Weather does not cover selected recovery horizon: {relative}")
    return tuple(intervals)


def _catalog_weather(files: _InputFiles, relative: str, layout_id: str,
                     weather_id: str, arrangement: str | None) -> tuple[str, str, str, datetime | None]:
    if arrangement is not None:
        raise InputError("Select a catalog weather case by weather_id and omit arrangement")
    rows = (row for row in files.table(relative) if _field(row, "layout_id") == layout_id)
    row = _select(_unique(rows, "weather_id"), weather_id, f"weather case for {layout_id}")
    time_basis = _field(row, "time_basis")
    if time_basis == "utc":
        origin = _utc(_field(row, "start_utc"))
    elif time_basis == "relative_seconds":
        if row.get("start_utc") != "":
            raise InputError("Relative weather requires a blank start_utc in the weather catalog")
        origin = None
    else:
        raise InputError(f"Unknown weather time basis: {time_basis!r}")
    return _field(row, "path"), _field(row, "role"), _field(row, "weather_source_id"), origin


def _demand_file(layout: Mapping, business: str, task_seed: int,
                 task_mix: str | None, *, uses_catalog: bool = False) -> tuple[str, str]:
    options = {}
    prefix = f"demand_{business}_"
    for relative in layout["demand_files"][business]:
        stem = Path(relative).stem
        if not stem.startswith(prefix):
            raise InputError(f"Unexpected demand filename: {relative}")
        mix, sep, seed = stem[len(prefix):].rpartition("_")
        if not mix or not sep or not seed.isascii() or not seed.isdigit():
            raise InputError(f"Missing mix or seed in demand filename: {relative}")
        key = (mix, int(seed))
        if key in options:
            raise InputError(f"Duplicate demand option {key}")
        options[key] = relative
    mixes = {mix for mix, seed in options if not uses_catalog or seed == task_seed}
    if task_mix is not None:
        selected = task_mix
    elif uses_catalog and len(mixes) == 1:
        selected = next(iter(mixes))
    else:
        selected = "mixed" if "mixed" in mixes else "mapped_mix"
    key = (selected, task_seed)
    if key not in options:
        raise InputError(f"Unknown task mix/seed {key}. Available: {sorted(options)}")
    return options[key], selected


def _demand(rows: Iterable[Mapping], business: str, end_s: float,
            assets: frozenset[str], templates: Mapping[str, tuple[UnitSpec, ...]]):
    batch = []
    periodic = []
    key = "task_id" if business == "batch" else "task_stream_id"
    streams = set()
    for identifier, row in _unique(rows, key).items():
        asset = _field(row, "asset_id")
        template = _field(row, "template_id")
        if asset not in assets or template not in templates:
            raise InputError(f"Unknown asset or inspection template in demand: {identifier}")
        weight = _positive(row, "priority_weight")
        if business == "batch":
            release = _number(row, "release_s", minimum=0)
            due = _number(row, "deadline_s", minimum=0, maximum=end_s)
            if release >= due:
                raise InputError(f"Expected release before deadline: {identifier}")
            batch.append(BatchTask(identifier, asset, template, release, due, weight))
        else:
            pair = (asset, template)
            if pair in streams:
                raise InputError(f"Duplicate periodic asset/template pair: {pair}")
            streams.add(pair)
            target = _positive(row, "target_interval_s")
            age = _number(row, "initial_age_s", minimum=0)
            periodic.append(PeriodicStream(identifier, asset, template, target, age, weight))
    return tuple(batch), tuple(periodic)


def _duration(setup: Mapping, name: str, *, allow_zero: bool = False) -> float:
    raw = setup[name]
    if type(raw) not in (int, float) or not math.isfinite(raw) or raw < 0 or (not allow_zero and raw == 0):
        raise InputError(f"Invalid scenario time setting {name}: {raw!r}")
    return float(raw)


def load_scenario(data_dir: Path, *, layout_id: str, business: str, fleet_id: str,
                  station_id: str, task_seed: int, weather_id: str,
                  arrangement: str | None = None, task_mix: str | None = None) -> Scenario:
    """Read one named scenario using the prepared index and explicit selectors.

    Catalog packages use an explicit weather ID for each layout. Legacy packages
    use either a historical block ID or a synthetic fixture and its arrangement.
    """
    if business not in {"batch", "periodic"}:
        raise InputError("business must be batch or periodic")
    if type(task_seed) is not int or task_seed < 0:
        raise InputError("task_seed must be a nonnegative integer")
    files = _InputFiles(Path(data_dir))
    index = files.index()
    try:
        return _load(files, index, layout_id, business, fleet_id, station_id,
                     task_seed, str(weather_id), arrangement, task_mix)
    except (KeyError, TypeError, AttributeError) as exc:
        raise InputError(f"Malformed prepared scenario index or table: {exc}") from exc


def _load(files, index, layout_id, business, fleet_id, station_id, task_seed,
          weather_id, arrangement, task_mix):
    layouts = _unique(index["layouts"], "layout_id")
    layout = _select(layouts, layout_id, "layout")
    tables = index["files"]
    setup = index["default_setup"]
    base_id, positions, allowed_legs, assets = _geometry(
        files, layout, files.table(tables["bases"])
    )
    profiles = {
        key: _profile(row)
        for key, row in _unique(files.table(tables["aircraft_profiles"]), "profile_id").items()
    }
    templates = _templates(files.table(tables["inspection_units"]))
    fleet_rows = files.table(tables["fleet_members"])
    fleet_ids = {_field(row, "fleet_id") for row in fleet_rows}
    _select(dict.fromkeys(fleet_ids), fleet_id, "fleet")
    fleet = tuple(
        FleetMember(key, _field(row, "profile_id"),
                    _number(row, "initial_soc_fraction", minimum=0, maximum=1))
        for key, row in _unique(
            (row for row in fleet_rows if _field(row, "fleet_id") == fleet_id), "aircraft_id"
        ).items()
    )
    station = _station(_select(_unique(files.table(tables["station_settings"]), "station_setting_id"),
                               station_id, "station setting"))
    for member in fleet:
        profile = _select(profiles, member.profile_id, "aircraft profile")
        if station.full_charge_soc_fraction * profile.battery_usable_wh <= profile.return_reserve_wh:
            raise InputError(f"Charging target does not clear return reserve: {profile.profile_id}")

    batch_end = _duration(setup, "batch_horizon_s")
    warmup = _duration(setup, "periodic_warmup_s", allow_zero=True)
    periodic_end = warmup + _duration(setup, "periodic_evaluation_s")
    evaluation_start, evaluation_end = (0.0, batch_end) if business == "batch" else (warmup, periodic_end)
    tail = _duration(setup, "recovery_only_tail_s", allow_zero=True)
    end_s = evaluation_end + tail
    uses_catalog = "weather_catalog" in tables
    demand_path, selected_mix = _demand_file(layout, business, task_seed, task_mix,
                                             uses_catalog=uses_catalog)
    batch, periodic = _demand(files.table(demand_path), business, evaluation_end, assets, templates)

    origin = None
    weather_role = "controlled_synthetic"
    source = None
    if uses_catalog:
        weather_path, weather_role, source, origin = _catalog_weather(
            files, tables["weather_catalog"], layout_id, weather_id, arrangement
        )
    elif layout_id == "SYN_MECH":
        if arrangement not in {"continuous", "fragmented"}:
            raise InputError("Synthetic weather requires a continuous or fragmented arrangement")
        matching = [r for r in files.table(tables["synthetic_weather_pairs"])
                    if _field(r, "arrangement") == arrangement]
        row = _select(_unique(matching, "fixture_id"), weather_id, "weather fixture")
        weather_path = _field(row, "path")
    else:
        if arrangement is not None:
            raise InputError("A weather arrangement applies only to the synthetic layout")
        rows = _unique(files.table(tables["historical_weather_blocks"]), "block_id")
        row = _select(rows, weather_id, "historical weather block")
        origin = _utc(_field(row, "start_utc"))
        for field, expected in (
            ("batch_end_utc", batch_end),
            ("periodic_evaluation_start_utc", warmup),
            ("periodic_end_utc", periodic_end),
            ("end_utc_with_recovery", periodic_end + tail),
        ):
            actual = (_utc(_field(row, field)) - origin).total_seconds()
            if actual != expected:
                raise InputError(f"Historical block time does not match scenario settings: {field}")
        weather_path = _field(row, "weather_path")
        weather_role = _field(row, "role")
        source = _field(row, "weather_source_id")
    weather = _weather(files, weather_path, origin, end_s, source, ordered_publications=uses_catalog)
    scenario_id = "-".join(
        (layout_id, business, fleet_id, station_id, selected_mix, str(task_seed),
         weather_id, arrangement or "replay")
    )
    provenance = {
        "input_schema": INPUT_SCHEMA,
        "inputs_sha256": files.combined_digest(),
        "layout_id": layout_id,
        "weather_id": weather_id,
        "weather_role": weather_role,
        "weather_path": weather_path,
        "weather_origin_utc": origin.isoformat() if origin is not None else "",
        "weather_arrangement": arrangement or "",
        "demand_path": demand_path,
        "task_mix": selected_mix,
        "task_seed": str(task_seed),
        "fleet_id": fleet_id,
        "station_id": station_id,
        "coordinate_system": _field(layout, "coordinate_system"),
        **{f"sha256:{relative}": digest for relative, digest in sorted(files.digests.items())},
    }
    return Scenario(
        scenario_id=scenario_id, business=business, base_id=base_id,
        positions=positions, allowed_legs=allowed_legs, profiles=profiles, templates=templates,
        fleet=fleet, station=station, weather=weather, batch_tasks=batch, periodic_streams=periodic,
        evaluation_start_s=evaluation_start, evaluation_end_s=evaluation_end, recovery_end_s=end_s,
        provenance=provenance,
    )
