"""Small configuration comparison; writes a fresh CSV, not the full paper study."""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

from island_uav.runner import default_config, run_simulation


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("outputs/comparison.csv"))
    parser.add_argument("--hours", type=float, default=6)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for fleet in (2, 4, 8):
        for targets in (1, 2):
            for ports, power in ((1, 240), (2, 120), (2, 240)):
                config = {**default_config(), "fleet_size": fleet,
                          "targets_per_sortie": targets, "charge_ports": ports,
                          "port_input_w": power, "duration_hours": args.hours}
                result = run_simulation(config)
                report = result["audit"]
                rows.append({"fleet_size": fleet, "sites_per_sortie": targets,
                             "charge_ports": ports, "port_input_w": power,
                             "completed_inspections": report["metrics"]["effective_completions"],
                             "average_aoi_h": report["metrics"]["mean_inspection_age_s"] / 3600,
                             "overdue_measure": report["metrics"]["standardized_overdue_exposure"],
                             "operating_energy_kwh": report["energy_summary"]["operation_consumed_wh"] / 1000,
                             "audit_status": report["status"]})
    with args.output.open("x", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"Saved {len(rows)} illustrative configurations to {args.output}")


if __name__ == "__main__":
    main()
