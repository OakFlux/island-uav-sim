"""Reconstruct service integrals independently from completed-inspection timestamps."""
from __future__ import annotations

import math
from collections import defaultdict


def stream_integrals(initial_age_s: float, interval_s: float, completions: list[float], start: float, end: float) -> dict:
    if not (0 <= start < end) or interval_s <= 0 or initial_age_s < 0:
        raise ValueError("Invalid time domain, interval or initial age")
    stamps = sorted(float(x) for x in completions)
    if not all(math.isfinite(t) for t in stamps):
        raise ValueError("Non-finite completion time")
    if len(stamps) != len(set(stamps)):
        raise ValueError("Duplicate completed inspections for the same demand and instant")
    if any(t < -1e-7 for t in stamps):
        raise ValueError("Negative completion time")
    last = -initial_age_s
    age_area, overdue_area = 0.0, 0.0
    left = start
    for t in stamps:
        if t < start:
            last = t
            continue
        if t > end:
            break
        age_area += .5 * ((t - last) ** 2 - (left - last) ** 2)
        overdue_area += (max(t - last - interval_s, 0) ** 2
                         - max(left - last - interval_s, 0) ** 2) / (2 * interval_s)
        left, last = t, t
    age_area += .5 * ((end - last) ** 2 - (left - last) ** 2)
    overdue_area += (max(end - last - interval_s, 0) ** 2
                     - max(left - last - interval_s, 0) ** 2) / (2 * interval_s)
    return {"mean_age_s": age_area / (end - start), "normalized_overdue": overdue_area / (end - start),
            "completed_in_evaluation": sum(start <= t <= end for t in stamps),
            "zero_updates": not any(start <= t <= end for t in stamps)}


def recompute_service(scenario: dict, completions: list[dict], subset: set[str] | None = None) -> dict:
    if scenario.get("business", "periodic") == "batch":
        return {"applicable": False, "reason": "batch service uses completion and due-date metrics"}
    start = float(scenario["clock"]["evaluation_start_s"])
    end = float(scenario["clock"]["evaluation_end_s"])
    by_task = defaultdict(list)
    known = {t["id"] for t in scenario["tasks"]}
    for c in completions:
        key = c.get("task_id", c.get("id"))
        if key not in known:
            raise ValueError(f"Unknown demand in completion ledger: {key}")
        by_task[key].append(c.get("time_s", c.get("t_s", c.get("t"))))
    rows = []
    for task in scenario["tasks"]:
        if subset is not None and task["id"] not in subset:
            continue
        if "interval_s" not in task:
            continue
        row = stream_integrals(float(task["initial_age_s"]), float(task["interval_s"]), by_task[task["id"]], start, end)
        rows.append({"task_id": task["id"], "template_id": task["template_id"], **row})
    if not rows:
        return {"applicable": False, "reason": "no periodic demand in requested subset"}
    return {"applicable": True, "mean_age_s": sum(r["mean_age_s"] for r in rows) / len(rows),
            "normalized_overdue": sum(r["normalized_overdue"] for r in rows) / len(rows),
            "zero_update_fraction": sum(r["zero_updates"] for r in rows) / len(rows),
            "completed_in_evaluation": sum(r["completed_in_evaluation"] for r in rows), "streams": rows}


def close(a: float, b: float, abs_tol=1e-5, rel_tol=1e-8) -> bool:
    return math.isclose(a, b, abs_tol=abs_tol, rel_tol=rel_tol)


def audit_result(scenario: dict, result: dict) -> dict:
    independent = recompute_service(scenario, result["completions"])
    checks = []
    metrics = result["metrics"]
    # The engine and this reader have separate integrators and only share timestamps.
    metric_names = {"mean_age_s": ("mean_age_s", "mean_aoi_s", "mean_age_seconds"),
                    "normalized_overdue": ("normalized_overdue", "overdue_index", "U")}
    if independent["applicable"]:
        for name, options in metric_names.items():
            key = next((x for x in options if x in metrics), None)
            if key is None:
                checks.append({"check": name, "pass": False, "reason": "engine metric missing"})
            else:
                checks.append({"check": name, "pass": close(independent[name], metrics[key]),
                               "independent": independent[name], "engine": metrics[key]})
    energy = result.get("energy_ledger", {})
    # Exact field binding is validated at integration; missing accounts never pass silently.
    keys = ("initial_wh", "charged_battery_wh", "consumed_wh", "terminal_wh")
    if all(k in energy for k in keys):
        residual = energy["initial_wh"] + energy["charged_battery_wh"] - energy["consumed_wh"] - energy["terminal_wh"] - energy.get("lost_wh", 0)
        checks.append({"check": "energy_balance", "pass": abs(residual) <= 1e-5, "residual_wh": residual})
    else:
        checks.append({"check": "energy_balance", "pass": False, "reason": "ledger fields missing", "keys": list(energy)})
    checks.extend(audit_event_accounts(scenario, result))
    return {"status": "pass" if all(c["pass"] for c in checks) else "fail", "checks": checks,
            "independent_service": independent}


def audit_event_accounts(scenario: dict, result: dict) -> list[dict]:
    """Replay ownership and independently derive phase power from the input model."""
    events = result.get("events", [])
    aircraft = {u["id"]: u for u in scenario["fleet"]}
    bases = {b["id"]: b for b in scenario["bases"]}
    tasks = {t["id"]: t for t in scenario["tasks"]}
    known = set(aircraft)
    accounts = defaultdict(list)
    holders = defaultdict(set)
    locks, assigned = {}, {}
    capacities = {("air", None): result["config"]["air_capacity"]}
    for bid, base in bases.items():
        capacities[("pad", bid)] = base["pad_capacity"]
        capacities[("charge", bid)] = base["charge_ports"]
    checks, issues = [], []
    last_time = -1e-7
    for event in events:
        t = event["time_s"]
        if not math.isfinite(t) or t < last_time - 1e-7:
            issues.append("non-finite or reversed event time")
        last_time = t
        uid = event.get("aircraft_id")
        if uid is not None and uid not in known:
            issues.append(f"unknown aircraft {uid}")
        kind = event["type"]
        if kind in {"phase_started", "instant_charge"}:
            accounts[uid].append(event)
        if kind == "task_assigned":
            task = event["task_id"]
            if task in locks or uid in assigned:
                issues.append(f"task/aircraft already locked {task} {uid}")
            locks[task], assigned[uid] = uid, task
        if kind == "task_released":
            task = event["task_id"]
            if locks.get(task) != uid or assigned.get(uid) != task:
                issues.append(f"task release without lock {task} {uid}")
            locks.pop(task, None)
            assigned.pop(uid, None)
        if kind in {"resource_acquired", "resource_released"}:
            resource = event["resource"]
            bid = None if resource == "air" else event.get("base_id")
            key = resource, bid
            if key not in capacities:
                issues.append(f"unknown resource {key}")
                continue
            if kind == "resource_acquired":
                if any(uid in value for (name, _), value in holders.items() if name == resource):
                    issues.append(f"duplicate resource acquisition {key} {uid}")
                holders[key].add(uid)
                if len(holders[key]) > capacities[key]:
                    issues.append(f"capacity exceeded {key}")
            else:
                if uid not in holders[key]:
                    issues.append(f"release without acquisition {key} {uid}")
                holders[key].discard(uid)
    air_phases = {"takeoff", "transit", "service", "service_ready", "decision", "return", "landing_wait", "landing", "arrived"}
    terminal = result.get("terminal", {})
    for row in terminal.get("aircraft", []):
        uid, phase, bid = row["id"], row["phase"], row["current_base_id"]
        expected = {(name, base) for (name, base) in capacities
                    if (name == "air" and phase in air_phases)
                    or (name == "pad" and base == bid and phase in {"takeoff", "landing"})
                    or (name == "charge" and base == bid and phase == "charging")}
        actual = {key for key, owners in holders.items() if uid in owners}
        if expected != actual:
            issues.append(f"terminal resource mismatch {uid}: {expected} vs {actual}")
    for row in terminal.get("tasks", []):
        if locks.get(row["id"]) != row.get("owner"):
            issues.append(f"terminal task owner mismatch {row['id']}")
    checks.append({"check": "resource_and_task_event_replay", "pass": not issues, "issues": issues[:20]})
    final_time = terminal.get("time_s")
    if final_time is None:
        return checks + [{"check": "phase_power_integral", "pass": False, "reason": "terminal time missing"}]
    a = scenario["clock"]["evaluation_start_s"]
    b = scenario["clock"]["evaluation_end_s"]
    totals = dict(consumed_wh=0., charged_battery_wh=0., grid_input_wh=0., evaluation_consumed_wh=0., recovery_consumed_wh=0.)
    phase_issues = []
    terminal_by_id = {x["id"]: x for x in terminal["aircraft"]}
    for uid, member in aircraft.items():
        profile = scenario["profiles"][member["profile_id"]]
        account = profile["battery_usable_wh"] * member["initial_soc"]
        left, power, battery_rate, grid_rate = 0., 0., 0., 0.
        rows = accounts[uid]
        if not rows or abs(rows[0]["time_s"]) > 1e-7:
            phase_issues.append(f"missing initial phase {uid}")
        for event in rows + [{"type": "audit_terminal", "time_s": final_time}]:
            right = event["time_s"]
            dt = right - left
            totals["consumed_wh"] += power * dt / 3600
            totals["charged_battery_wh"] += battery_rate * dt / 3600
            totals["grid_input_wh"] += grid_rate * dt / 3600
            totals["evaluation_consumed_wh"] += power * max(0, min(right, b) - max(left, a)) / 3600
            totals["recovery_consumed_wh"] += power * max(0, right - max(left, b)) / 3600
            account += (battery_rate - power) * dt / 3600
            if not math.isfinite(account) or account < -1e-6 or account > profile["battery_usable_wh"] + 1e-6:
                phase_issues.append(f"battery outside physical capacity {uid}@{right}")
            if event["type"] == "instant_charge":
                amount = event["battery_wh"]
                base = bases[event["base_id"]]
                efficiency = base["charge_efficiency"]
                expected_amount = max(0, base["target_soc"] * profile["battery_usable_wh"] - account)
                if not close(amount, expected_amount):
                    phase_issues.append(f"instant charge target mismatch {uid}@{right}")
                if amount < 0 or not close(event["grid_wh"], amount / efficiency):
                    phase_issues.append(f"instant charging efficiency mismatch {uid}@{right}")
                account += amount
                if not math.isfinite(account) or account > profile["battery_usable_wh"] + 1e-6:
                    phase_issues.append(f"instant charge exceeds capacity {uid}@{right}")
                totals["charged_battery_wh"] += amount
                totals["grid_input_wh"] += amount / efficiency
                if not close(event["energy_wh"], account):
                    phase_issues.append(f"instant battery discontinuity {uid}@{right}")
            elif event["type"] == "phase_started":
                if not close(event["energy_wh"], account):
                    phase_issues.append(f"phase battery discontinuity {uid}@{right}")
                phase = event["phase"]
                field = {"takeoff": "takeoff_w", "transit": "cruise_w", "return": "cruise_w",
                         "landing": "landing_w", "landing_wait": "hold_w"}.get(phase, "ground_w")
                expected_power = 0. if phase in {"charging", "failed"} else profile[field]
                if phase == "service":
                    task_id, step_index = event.get("task_id"), event.get("step_index")
                    if task_id not in tasks or step_index is None:
                        phase_issues.append(f"missing service identity {uid}@{right}")
                        expected_power = event.get("power_w", 0)
                    else:
                        step = scenario["templates"][tasks[task_id]["template_id"]][step_index]
                        expected_power = profile["inspection_base_w"] * step["power_factor"]
                expected_battery, expected_grid = 0., 0.
                if phase == "charging":
                    base = bases[event["base_id"]]
                    expected_battery = min(base["charge_efficiency"] * base["port_input_w"], profile["charge_intake_battery_w"])
                    expected_grid = expected_battery / base["charge_efficiency"]
                for key, value in (("power_w", expected_power), ("charge_battery_w", expected_battery), ("charge_grid_w", expected_grid)):
                    if not close(event.get(key, 0), value):
                        phase_issues.append(f"input-derived {key} mismatch {uid}@{right}")
                power, battery_rate, grid_rate = expected_power, expected_battery, expected_grid
            left = right
        if not close(account, terminal_by_id[uid]["energy_wh"]):
            phase_issues.append(f"terminal battery mismatch {uid}")
    for key, value in totals.items():
        if key not in result["energy_ledger"] or not close(value, result["energy_ledger"][key]):
            phase_issues.append(f"{key}: independent {value}, engine {result['energy_ledger'].get(key)}")
    checks.append({"check": "phase_power_integral", "pass": not phase_issues, "issues": phase_issues[:20], "independent": totals})
    return checks
