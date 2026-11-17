# Running the model

Python 3.10 or newer is required. The simulation and local web server use the Python standard library. Install from the repository root:

```sh
python -m pip install -e .
island-uav serve --open
```

The local Python server executes the physical simulation; the browser replays its recorded aircraft positions in 2D. The default is a six-hour, 96-location, 12-aircraft **demonstration with synthetic wind**, three bases and near-base recovery. Its task ages start at their target intervals so that aircraft are active immediately. It is not a formal paper result.

## A configuration file

```sh
island-uav run --config examples/demo.json --output runs/demo --replay
```

Omitting `--config` selects the same default demo. Output folders containing an existing simulation are not overwritten. `examples/compact.json` selects the single-base network. `examples/historical_network.json` uses the supplied historical block and paper initial ages. The latter has the same physical inputs as the selected network reference, apart from identifying metadata; use `reproduce` for the unchanged archived input.

| Key | Choices or meaning |
| --- | --- |
| `network_id` | `legacy_24`: 24 locations and one base; `pingtan_48`, `pingtan_96`, `pingtan_192`: three bases |
| `fleet_size` | 2, 4 or 8 for `legacy_24`; 6, 12 or 24 for the three-base networks |
| `targets_per_sortie` | 1 or 2 distinct inspection locations per sortie |
| `charge_ports` | 1 or 2 ports **per base** |
| `port_input_w` | Input power of each port, in W; 120 or 240 reproduce the study designs |
| `policy` | `B0`: urgency-based reference; `B2`: completion-aware overdue-area reference |
| `recovery_policy` | `HOME`, `NEAR` or `QUEUE` |
| `weather` | `synthetic` or `historical` |
| `duration_hours` | Evaluation length, up to 72 h |
| `warmup_hours` | Prior initialization period, 0–24 h; demo default 0 |
| `recovery_hours` | Extra time to recover aircraft already flying, default 1 h; no new sorties |
| `task_seed` | Prepared demand allocation 101, 202 or 303 |
| `initial_age_mode` | `due` for active demonstrations; `paper` preserves the prepared initial ages |
| `historical_block` | Only `2025-04-04` is bundled |
| `historical_start_hour` | Offset into the bundled 97-hour historical block, default 0 |
| `weather_arrangement` | `variable` normally; synthetic-only `continuous`/`fragmented` illustrate equal-share openings of different lengths |
| `execution_mode` | `full` normally; `accumulating` and `instant_charge` are deliberate mechanism simplifications |

The fleet is divided equally among the bases; each base retains the prepared alternating light/general aircraft sequence. C0 is one 240 W port per base; C1 is two 120 W ports; C2 is two 240 W ports. Charging efficiency and aircraft intake limits remain active. Public validation rejects unsupported keys and invalid combinations. Historical warm-up, evaluation and recovery together must fit within the available 97 hours.

B2 ranks a bounded set of one-/two-target candidate plans by the probability-weighted reduction in priority-weighted overdue-age area over a two-hour look-ahead horizon. It breaks ties by estimated post-recovery service-readiness time, then energy and task identifiers; it executes only the first target before reconsidering the observed state. Historical opening-spell samples come only from 2023–2024; dispatch observes the current regional wind and observed opening age, not future evaluation weather. HOME returns to the assigned home base. NEAR selects a feasible base by estimated flight time. QUEUE adds current base-resource backlog and own replenishment time. These are interpretable reference rules; they are not a global scheduling optimizer.

## Selected historical reference cases

```sh
island-uav reproduce --case all --output runs/references
```

Or choose `--case compact` / `--case network`. The command loads the **unchanged archived assembled input**, runs the frozen executor, reconstructs the collection/energy/resource accounts and platform-delivery accounts independently, and compares every recorded metric against the selected archive. It takes around 20 seconds on the development machine; hardware affects time.

| Case | Archived run | Configuration |
| --- | --- | --- |
| `compact` | `E1B-B04-W20250404-S101` | 24 locations, 4 UAVs, one base, C0, B2, HOME, two locations per sortie |
| `network` | `E2-M04-W20250404-S101` | 96 locations, 12 UAVs, three bases, C0, B2, NEAR, two locations per sortie |

Both use 4 April 2025 onward, 24 h of warm-up, 72 h of evaluation, and a 1 h recovery tail. These two configurations demonstrate reproduction of selected study inputs and outputs. The complete 1,900-run experimental archive is not distributed here.

The network reference preserves its two recorded operational safety flags. An accounting audit that passes means that the recorded service, power and resource states agree with independent reconstructions; it does not certify an operation as safe or erase adverse outcomes.

## Outputs and interpretation

Each saved run contains:

- `config.json`: validated public options; `execution_config.json`: executor options.
- `scenario.json`: the complete physical input, including grid-axis wind intervals.
- `physical.json`: physical events, completions, terminal states, metrics and energy ledger.
- `events.jsonl`: the same event sequence as individual JSON records.
- `delivery.json`: accepted-step carriage, unloading, complete-generation arrival, and both age measures.
- `audit.json`: independent physical and delivery checks.
- `metadata.json`: map coordinates and labels, kept separate from execution.
- `manifest.json`: schema, timing, scope and hashes of saved input/output files.
- `reference_comparison.json`: added by `reproduce`; `replay.json`: added by `--replay`.

Time, distance, power and energy are stored in s, m, W and Wh. Age is integrated over the evaluation interval. Operational energy includes evaluation **and the recovery tail**, without including warm-up. Some event counters and accumulated queue times describe the whole simulated run; they should not be silently interpreted as evaluation-only rates. Read the explicit cohort definitions in `delivery.json` when comparing collected and delivered counts.

Collection age resets when every required inspection step has been accepted. Each accepted step remains with the aircraft that acquired it; successful landing at any modeled base unloads it instantaneously. The platform receives a complete update only when all steps from that generation have arrived, even when they were collected by different aircraft or delivered at different bases. Its timestamp remains the original complete-acquisition time. Upload duration, bandwidth, energy and dispatch feedback are not modeled. The `instant` delivery calculation is an idealized accounting reference, not an additional radio simulator.

```sh
island-uav audit runs/demo
python -m unittest discover -s tests
```

`audit` recomputes accounts from saved physical and delivery traces and checks recorded hashes. The fast tests cover coordinate frames, directional flight, resource accounting, configuration, network selection and cross-aircraft delivery age. Historical replay is exercised by the `reproduce` command, or optionally by `ISLAND_UAV_REFERENCE_TESTS=1 python -m unittest discover -s tests`.

## Using a different island

The executor is independent of Pingtan. A prepared scenario supplies `positions`, regional `weather_zones`, `weather`, `bases`, `profiles`, `fleet`, `templates`, `tasks`, `clock` and optionally `allowed_legs`. For another island, prepare these dictionaries in a common metric coordinate frame and supply wind components in that same frame. Templates are ordered continuous steps; task intervals and initial ages define repeated demand. An omitted `allowed_legs` means complete straight-line connections; the supplied compact case instead retains its 546 allowed directed connections.

```python
from island_sim_v5 import simulate
from island_uav.core.delivery import evaluate_delivery
from island_uav.core.independent_audit import audit_result

physical = simulate(scenario, {
    "policy": "B0", "recovery": "NEAR", "max_targets_per_sortie": 2,
    "execution_mode": "full", "record_events": True, "air_capacity": 24,
})
accounts = audit_result(scenario, physical)
delivery = evaluate_delivery(scenario, physical, mode="base")
```

The bundled browser configuration is restricted to the supplied Pingtan networks; adding a new island to the browser also requires geographic display metadata. Do not reuse Pingtan's geographic wind-frame table or historical reliability samples for a different island. Base sites, permission constraints and aircraft parameters should be selected for the new research setting. These scenarios are research inputs, not flight approvals.
