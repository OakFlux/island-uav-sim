# Included inputs and reproducibility

The repository contains a runnable subset of the study, rather than every
experimental record. Python code has no runtime dependencies outside the
standard library. Installed wheels contain the same inputs as the source tree.

## Inputs

Packaged files are in `src/island_uav/data/`:

| File | Content | Source and status |
| --- | --- | --- |
| `assets.csv` | 24 projected and geographic inspection positions | Eight OSM mapped anchors; sixteen observation stations inferred along mapped roads. Inferred stations are not verified facilities. |
| `legs.csv` | 273 admissible undirected straight links | Scenario links screened against the prepared island geometry; not operational flight permissions. |
| `bases.csv` | One base at 25.490 N, 119.735 E | Assumed scenario base. |
| `profiles.csv`, `templates.csv` | Two aircraft types and S/L/M sensing steps | Specified research profiles and procedures; not manufacturer calibration. |
| `demand_101.csv` etc. | Three recurring-demand realizations | Synthetic intervals, priorities and initial ages. |
| `historical_wind.csv` | 97 hourly regional wind vectors from 2025-04-04 00:00 UTC | Open-Meteo / ECMWF ERA5 regional 10 m proxy. It does not resolve local turbulence. |
| `development_spells.json` | Complete suitable-weather spell durations from 2023/2024 | Exact frozen `WeatherReliability.from_development` output, preserving B2's empirical probability calculation without bundling two full years of hourly data. Source file hashes are included. |
| `provenance.json` | Input and frozen-core hashes | Original source-relative filenames, transformations and attribution. |
| `paper_case.json`, `reference.json` | One paper configuration and expected metrics | Original run `N2-0079`, described below. |

OpenStreetMap-derived geographic inputs are available under
[ODbL 1.0](https://www.openstreetmap.org/copyright):
© OpenStreetMap contributors, through the
[Geofabrik Fujian extract](https://download.geofabrik.de/asia/china/fujian.html),
2026-01-01 snapshot. Derived database content remains under ODbL; the code
license does not replace that data license. Retain attribution when using or
redistributing these inputs.

Wind and derived spell data are attributed to Open-Meteo and ECMWF/Copernicus
ERA5 under [CC BY 4.0](https://open-meteo.com/en/licence). The original source
was Open-Meteo's historical API at the model grid selected for the study
(25.50 N, 119.75 E); the acquisition requested the nearby Pingtan location
25.517 N, 119.783 E, with `models=era5`, `wind_speed_unit=ms`, `timezone=UTC`,
`cell_selection=nearest` and `elevation=nan`. No API calls are needed to run.
Task, aircraft and base settings are scenario assumptions authored for the study.

## Demonstration versus paper case

`examples/pingtan.json` is a six-hour interactive illustration. It uses explicit
synthetic wind and sets every initial information age to its target interval,
so recurring work is available immediately. It is useful for watching the
operating cycle; its metrics are not the aggregate paper results.

`examples/historical.json` uses the actual bundled hourly wind, beginning
24 hours into the historical block, with the original demand initial ages.
The earlier hours of that block include strong wind. The short display begins
at a period suitable for more task types; it does not alter the recorded wind.

`examples/paper_case.json` restores the original conditions for `N2-0079`:

- Four UAVs, alternating light RGB and general RGB/zoom aircraft.
- At most two distinct sites per sortie; B2 limited-lookahead dispatch.
- Two 240 W charging ports, one pad and airborne capacity eight.
- Demand seed 101, original initial ages, original 24 positions and 273 links.
- The full historical block from 2025-04-04 00:00 UTC; 24 h warm-up,
  72 h assessment and 1 h recovery.

Its reference has 401 completed inspections, mean AoI 11535.169821571913 s,
overdue measure 0.3936290768050177 and 31740 execution events. The public run's
21 audit fields other than its new scenario identifier match the frozen
original exactly on the development runtime. Numeric tolerance is appropriate
across Python/platform versions. This reproduces one paper case, not its full
multi-block estimates, confidence intervals or configuration-selection study.

The frozen `engine.py`, `domain.py`, `physics.py`, `policies.py`, `inputs.py`,
`audit.py`, `ledger.py` and `__init__.py` are byte-identical to the formal study
sources. The public runner adapts portable JSON configuration and adds
output-only position telemetry. It does not change decisions or physics.
`position_xy` is in EPSG:32650 meters; on an `energy_interval` event it is the
position at `end_s`. Intermediate playback positions can be interpolated
linearly within that interval, as the simulation does.

## Configuration and outputs

Copy an example JSON and change fleet size (2/4/8), sites per sortie (1/2),
policy (B0/B2), charging ports (1/2), power per port, weather and horizon.
Port power is a per-port allocation: two 120 W ports preserve the same total
rated supply as one 240 W port; two 240 W ports double it. The simulator uses
fixed allocations and aircraft-specific charging acceptance limits.

A run folder contains `config.json`, `scenario.json`, `metadata.json`,
`manifest.json`, `events.jsonl`, `audit.json` and `status.json`.
The scenario snapshot makes a saved-run audit independent of later changes to
the packaged inputs. Final AoI, overdue service, energy and resource metrics
come from the independent event audit. The map is a display of the same
projected geometry, rather than a separate flight simulator.

For a small illustrative configuration comparison:

```bash
python examples/compare_configs.py --output outputs/comparison.csv
```

This evaluates 18 configurations under common synthetic weather and demand.
It illustrates resource trade-offs; it does not replace the full paper study.

For a new island, prepare matching site/base coordinates in meters and
longitude/latitude, admissible links, aircraft and sensing profiles, recurring
demand, and time-indexed wind. The current public runner selects the bundled
Pingtan case; importing arbitrary island datasets requires a new adapter around
the same `Scenario` records. No claim of automatic geographic import is made.

## Offline display map

`src/island_uav/web/map.geojson` contains a compact derivative of the same OSM
extract: the complete island coastline, plus roads and land-use polygons in the
service area. It is geographic source data rendered by the browser, not an
AI-generated map or a stored tile-service archive. `docs/map_provenance.json`
records clipping, simplification and source hashes. Preserve its OSM attribution
and ODbL licence independently of the MIT software licence.
