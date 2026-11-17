"""Independent offline reconstruction and integration of delivery-side freshness.

This checker does not import delivery.py or its integration helpers. It binds
step events to later completion records and independently finds the first
subsequent landing/failure of each carrying aircraft.
"""
from __future__ import annotations

from collections import defaultdict
from math import isclose, isfinite


def _same(left, right):
    return isclose(float(left), float(right), abs_tol=1e-5, rel_tol=1e-9)


def _integrate(initial, reports, start, end, side):
    trigger = "generation_time_s" if side == "collection" else "delivery_time_s"
    available = [r for r in reports if r[trigger] is not None]
    cuts = sorted({start, end} | {float(r[trigger]) for r in available if start < r[trigger] < end})
    area = 0.
    for left, right in zip(cuts, cuts[1:]):
        middle = (left + right) / 2
        timestamp = max([initial] + [r["generation_time_s"] for r in available if r[trigger] <= middle])
        area += (right-left) * (middle-timestamp)
    return area / (end-start)


def audit_delivery(scenario: dict, result: dict, delivery: dict) -> dict:
    checks, problems = [], []
    events = result["events"]
    mode = delivery["mode"]
    if mode not in {"base", "instant"}:
        return {"status": "fail", "checks": [{"check": "mode", "pass": False}]}
    tasks = {t["id"]: t for t in scenario["tasks"]}
    end = float(result["terminal"]["time_s"])
    start = float(scenario["clock"]["evaluation_start_s"])
    stop = float(scenario["clock"]["evaluation_end_s"])
    completions, carrier_endings = defaultdict(list), defaultdict(list)
    for index, event in enumerate(events):
        if not isfinite(event["time_s"]):
            raise ValueError("Non-finite event timestamp")
        if event["type"] == "inspection_completed":
            completions[event["task_id"]].append((index, event))
        if event["type"] == "aircraft_failed" or event["type"] == "sortie_ended" and event.get("outcome") == "landed":
            carrier_endings[event["aircraft_id"]].append((index, event))
    steps, records = {}, {}
    for task_id, items in completions.items():
        for number, (_, event) in enumerate(items, 1):
            if event["generation"] != number:
                problems.append(f"unexpected completion generation {task_id}")
            records[task_id, number] = {"task_id": task_id, "generation": number,
                                      "generation_time_s": event["time_s"]}
    for index, event in enumerate(events):
        if event["type"] != "step_completed":
            continue
        task_id = event["task_id"]
        if task_id not in tasks:
            raise ValueError("Unknown task in steps")
        future = [(rank, e) for rank, e in completions[task_id] if rank >= index]
        generation = future[0][1]["generation"] if future else len(completions[task_id]) + 1
        key = task_id, generation, event["step_index"]
        if key in steps:
            problems.append(f"duplicate step {key}")
        uid = event["aircraft_id"]
        if mode == "instant":
            status, at, base = "delivered", event["time_s"], None
        else:
            after = [e for rank, e in carrier_endings[uid] if rank > index]
            if not after:
                status, at, base = "pending", None, None
            elif after[0]["type"] == "aircraft_failed":
                status, at, base = "lost", None, None
            else:
                status, at, base = "delivered", after[0]["time_s"], after[0]["base_id"]
        steps[key] = {"task_id": task_id, "generation": generation, "step_index": event["step_index"],
                      "acquisition_time_s": event["time_s"], "aircraft_id": uid,
                      "status": status, "delivery_time_s": at, "delivery_base_id": base}
    given_steps = {(r["task_id"], r["generation"], r["step_index"]): r for r in delivery["step_ledger"]}
    if len(given_steps) != len(delivery["step_ledger"]) or set(given_steps) != set(steps):
        problems.append("step ledger key mismatch")
    for key in set(steps) & set(given_steps):
        expected, given = steps[key], given_steps[key]
        for field in ("acquisition_time_s", "aircraft_id", "status", "delivery_time_s"):
            if expected[field] != given[field] and not (field.endswith("_s") and expected[field] is not None
                                                       and given[field] is not None and _same(expected[field], given[field])):
                problems.append(f"step mismatch {key} {field}")
        if mode == "base" and expected["delivery_base_id"] != given["delivery_base_id"]:
            problems.append(f"step base mismatch {key}")
    by_task = defaultdict(list)
    for (task_id, generation), record in records.items():
        nsteps = len(scenario["templates"][tasks[task_id]["template_id"]])
        payloads = [steps.get((task_id, generation, j)) for j in range(nsteps)]
        if any(p is None for p in payloads):
            problems.append(f"completed report lacks steps {(task_id, generation)}")
            continue
        if all(p["status"] == "delivered" for p in payloads):
            at = max(p["delivery_time_s"] for p in payloads)
            status = "delivered"
        else:
            at = None
            status = "lost" if any(p["status"] == "lost" for p in payloads) else "pending"
        record.update(delivery_time_s=at, status=status)
        by_task[task_id].append(record)
    supplied = {(r["task_id"], r["generation"]): r for r in delivery["inspection_ledger"]}
    if len(supplied) != len(delivery["inspection_ledger"]) or set(supplied) != set(records):
        problems.append("inspection ledger key mismatch")
    for key in set(records) & set(supplied):
        for field in ("generation_time_s", "delivery_time_s", "status"):
            e, g = records[key].get(field), supplied[key].get(field)
            if e != g and not (field.endswith("_s") and e is not None and g is not None and _same(e, g)):
                problems.append(f"inspection mismatch {key} {field}")
    expected_updates = sorted((r for r in records.values() if r.get("delivery_time_s") is not None),
                              key=lambda r: (r["delivery_time_s"], r["task_id"], r["generation_time_s"], r["generation"]))
    given_updates = delivery["platform_updates"]
    if len(expected_updates) != len(given_updates):
        problems.append("platform update count mismatch")
    latest = {task_id: -float(task.get("initial_age_s", 0)) for task_id, task in tasks.items()}
    for expected, given in zip(expected_updates, given_updates):
        task_id = expected["task_id"]
        previous = latest[task_id]
        accepted = expected["generation_time_s"] > previous
        latest[task_id] = max(previous, expected["generation_time_s"])
        for field in ("task_id", "generation", "generation_time_s", "delivery_time_s"):
            if expected[field] != given[field]:
                problems.append(f"platform update identity mismatch {task_id} {field}")
        if (given["accepted"] != accepted or not _same(given["previous_timestamp_s"], previous)
                or not _same(given["latest_timestamp_s"], latest[task_id])):
            problems.append(f"platform update ordering mismatch {task_id}")
    checks.append({"check": "independent_payload_reconstruction", "pass": not problems, "issues": problems[:20]})
    per_task = {r["task_id"]: r for r in delivery["per_task"]}
    metric_issues, monotonic_issues = [], []
    age_col, age_plat = [], []
    for task_id, task in tasks.items():
        reports = by_task[task_id]
        initial = -float(task.get("initial_age_s", 0))
        col = _integrate(initial, reports, start, stop, "collection")
        plat = _integrate(initial, reports, start, stop, "platform")
        age_col.append(col);age_plat.append(plat)
        row = per_task.get(task_id)
        if row is None:
            metric_issues.append(f"missing per-task metrics {task_id}")
        else:
            for field, expected in (("collection_mean_aoi_s", col), ("platform_mean_aoi_s", plat), ("mean_aoi_gap_s", plat-col)):
                if not _same(row[field], expected):
                    metric_issues.append(f"per-task age mismatch {task_id} {field}")
        cuts = {0., start, stop, end} | {r["generation_time_s"] for r in reports}
        cuts |= {r["delivery_time_s"] for r in reports if r["delivery_time_s"] is not None}
        cuts = sorted(cuts)
        samples = cuts + [(l+r)/2 for l,r in zip(cuts,cuts[1:])]
        for at in samples:
            lc = max([initial] + [r["generation_time_s"] for r in reports if r["generation_time_s"] <= at])
            lp = max([initial] + [r["generation_time_s"] for r in reports if r["delivery_time_s"] is not None and r["delivery_time_s"] <= at])
            if lp > lc + 1e-7:
                monotonic_issues.append(f"platform newer than acquired {task_id}@{at}")
        for r in reports:
            if r["delivery_time_s"] is not None and r["delivery_time_s"] < r["generation_time_s"]-1e-7:
                monotonic_issues.append(f"delivery before complete acquisition {task_id}")
    metrics = delivery["metrics"]
    cmean, pmean = sum(age_col)/len(tasks), sum(age_plat)/len(tasks)
    flat = list(records.values())
    expected_counts = {
        "collection_complete_count": sum(start <= r["generation_time_s"] <= stop for r in flat),
        "delivered_count": sum(r["delivery_time_s"] is not None and start <= r["delivery_time_s"] <= stop for r in flat),
        "eval_cohort_delivered_by_end": sum(start <= r["generation_time_s"] <= stop and r["delivery_time_s"] is not None and r["delivery_time_s"] <= stop for r in flat),
        "eval_cohort_undelivered_at_end": sum(start <= r["generation_time_s"] <= stop and (r["delivery_time_s"] is None or r["delivery_time_s"] > stop) for r in flat),
        "undelivered_at_eval_end": sum(r["generation_time_s"] <= stop and (r["delivery_time_s"] is None or r["delivery_time_s"] > stop) for r in flat),
        "lost_inspections": sum(r.get("status") == "lost" for r in flat),
        "pending_inspections_at_end": sum(r.get("status") == "pending" for r in flat)}
    for field, expected in {"collection_mean_aoi_s": cmean, "platform_mean_aoi_s": pmean,
                            "mean_aoi_gap_s": pmean-cmean, **expected_counts}.items():
        if field not in metrics or not _same(metrics[field], expected):
            metric_issues.append(f"aggregate metric mismatch {field}")
    delivered = [r for r in flat if r["delivery_time_s"] is not None and start <= r["delivery_time_s"] <= stop]
    expected_delay = sum(r["delivery_time_s"]-r["generation_time_s"] for r in delivered)/len(delivered) if delivered else None
    if (expected_delay is None and metrics.get("mean_delivery_delay_s") is not None
            or expected_delay is not None and (metrics.get("mean_delivery_delay_s") is None
                                                or not _same(expected_delay, metrics["mean_delivery_delay_s"]))):
        metric_issues.append("conditional delivery delay mismatch")
    checks.append({"check": "independent_piecewise_age_and_counts", "pass": not metric_issues,
                   "issues": metric_issues[:20], "collection_mean_aoi_s": cmean,
                   "platform_mean_aoi_s": pmean, "mean_aoi_gap_s": pmean-cmean})
    checks.append({"check": "pointwise_information_ordering", "pass": not monotonic_issues,
                   "issues": monotonic_issues[:20]})
    return {"status": "pass" if all(c["pass"] for c in checks) else "fail", "checks": checks,
            "independent_records": len(records), "independent_steps": len(steps)}
