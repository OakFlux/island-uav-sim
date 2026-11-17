"""Re-execute two archived historical inputs and compare all published accounts."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from .runner import DATA, _execute, _read, _write


def reference_cases() -> dict:
    return _read(DATA / "references/index.json")["cases"]


def reference_config(case: str) -> dict:
    from .runner import load_config
    ref = reference_cases()[case]
    spec = ref["spec"]
    ports, power = {"C0":(1,240),"C1":(2,120),"C2":(2,240)}[spec["config"]["charging"]]
    return load_config(dict(network_id=spec["network_id"], fleet_size=spec["config"]["fleet_size"],
        targets_per_sortie=spec["config"]["max_targets_per_sortie"], charge_ports=ports,port_input_w=power,
        policy=spec["config"]["task_policy"],recovery_policy=spec["config"]["recovery_policy"],
        weather="historical", duration_hours=spec["evaluation_s"]/3600,
        warmup_hours=spec["warmup_s"]/3600,recovery_hours=spec["recovery_s"]/3600,
        task_seed=spec["demand_seed"], initial_age_mode="paper"))


def compare_reference(result: dict, case: str) -> dict:
    reference = reference_cases()[case]
    rows = []
    for section, reference_key in (("physical","physical_metrics"),("delivery","delivery_metrics")):
        for key, expected in reference[reference_key].items():
            actual = result[section]["metrics"].get(key)
            same = (type(actual) in (int,float) and math.isclose(actual,expected,rel_tol=1e-10,abs_tol=1e-6)
                    if type(expected) in (int,float) else actual == expected)
            rows.append(dict(metric=f"{section}.{key}",expected=expected,actual=actual,match=same))
    accounts = result["audit"]["status"] == "pass"
    return dict(status="pass" if all(row["match"] for row in rows) and accounts else "fail",
                reference_case=case,source_run_id=reference["run_id"],accounting_pass=accounts,
                scope="One selected archived historical input, not the full experimental study.",checks=rows)


def run_reference(case: str, output: str | Path | None = None) -> dict:
    if case not in reference_cases():
        raise ValueError("Reference case must be 'compact' or 'network'")
    reference = reference_cases()[case]
    path = DATA / f"references/{case}_input.json"
    if hashlib.sha256(path.read_bytes()).hexdigest() != reference["input_sha256"]:
        raise ValueError("The archived reference input has changed")
    prepared = _read(path)
    # Do not reconstruct these inputs: exact prepared graphs, fleet order, clocks,
    # rotated weather, and reliability samples remain those of the archive.
    result = _execute(prepared["scenario"],prepared["config"],reference_config(case),output,reference=case)
    result["reference_comparison"] = compare_reference(result,case)
    if output is not None:
        _write(Path(output)/"reference_comparison.json",result["reference_comparison"])
    return result
