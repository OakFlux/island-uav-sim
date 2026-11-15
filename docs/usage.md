# Usage

Install Python 3.10 or later, then run `python -m pip install -e .` from the repository directory. There are no runtime dependencies, map keys, JavaScript build steps or external services. A virtual environment is recommended. Windows users can use `py` in place of `python`.

## Browser

Run `island-uav serve` and open http://127.0.0.1:8000. `--open` opens the browser automatically; `--port 8080` changes the port. Ctrl+C stops the server. The server binds to the local computer by default.

Choose 2, 4 or 8 aircraft, one or two sites per sortie, charging ports and power, dispatch policy, weather and duration. **Run simulation** executes the model again; Play, the speed selector and the timeline explore the resulting event trajectory. The interface interpolates motion between observed simulation events. It does not change flight, sensing or decision rules. Select a site to inspect its task type and revisit requirement; drag or scroll to pan or zoom. The JSON download contains the displayed trajectory and independent audit summary.

The default uses controlled synthetic wind to make weather interruptions visible. Historical weather is a separate selection using an actual regional wind block. A run includes its selected inspection horizon followed by a one-hour recovery period; no new inspections are dispatched after the inspection horizon closes.

**Displayed indicators:** completed inspections and energy are cumulative from simulation start. Current mean information age (AoI) and overdue ratio summarize the requirements at the selected instant. These time-varying displays differ from the time-averaged evaluation-period measures in the independent audit. A complete inspection, not a visit or individual view, resets information age.

## JSON configurations and run outputs

Copy `examples/pingtan.json` and change its fields. Common options are:

| Field | Meaning |
|---|---|
| `fleet_size` | 2, 4 or 8 aircraft; equal shares of light RGB and general RGB/zoom aircraft |
| `targets_per_sortie` | Maximum distinct sites per sortie: 1 or 2 |
| `charge_ports`, `port_input_w` | Charging ports and fixed input power **per port**, in watts |
| `policy` | `B0` priority-based dispatch; `B2` limited lookahead |
| `weather` | `synthetic` or the bundled `historical` block |
| `duration_hours`, `warmup_hours`, `recovery_hours` | Evaluation duration, warm-up and recovery |
| `weather_arrangement` | `variable`, `continuous` or `fragmented` synthetic wind |
| `task_seed` | One of three bundled demand tables: 101, 202 or 303 |
| `initial_age_mode` | `due` for the short demonstration; `paper` for original initial ages |

`island-uav run --config examples/historical.json --output outputs/historical --replay` saves:

- `config.json` and `scenario.json`: resolved options and complete model inputs.
- `manifest.json`: input, code and development-weather provenance.
- `events.jsonl`: complete event ledger, including output-only position observations.
- `audit.json` and `status.json`: independently reconstructed results and audit status.
- `replay.json` when `--replay` is supplied: browser trajectory.

Choose a new output directory for each run. Existing results are never overwritten. Example demand and facility assignments are semi-synthetic; mapped locations do not establish that an asset owner commissioned a particular inspection.

## Selected reproduction

`island-uav reproduce --output outputs/paper-case` repeats one archived full-horizon configuration: 24-hour warm-up, 72-hour evaluation and one-hour recovery under historical regional wind. It compares the resulting audit against the original case identified in `src/island_uav/data/reference.json`, and writes `reference_comparison.json`. All compared values must match. This small repository does not contain every experiment or support recomputing the full paper's statistical figures.

`python examples/compare_configs.py --help` provides a small new configuration comparison. Its outputs illustrate how to design further experiments; they are not replacement estimates for the paper's complete study.

## Model and extensions

The original discrete-event execution, dispatch and independent audit modules are retained. The public adapter loads JSON and compact CSV inputs; the browser is a read-only view of resulting event records. Inspection requirements include short observations, continuous long observations and ordered multiview observations. Completed views persist, but an interrupted active view restarts. Travel is on specified straight links with wind-dependent ground speed and phase-based energy, with shared base resources and charging queues.

The bundled adapter targets the Pingtan case (WGS84 / UTM zone 50N). Other study areas require corresponding sites, legs, base and demand CSV inputs, appropriate weather and profiles, plus matching geographic display data and projection. The underlying engine accepts these objects; the current browser presets are Pingtan configurations.

For development checks: `python -m unittest discover -s tests -v`. The command-line interface also works as `python -m island_uav`.
