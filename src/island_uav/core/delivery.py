"""Passive data-delivery accounting on an unchanged physical simulation trace.

Accepted step outputs are carried by their acquiring aircraft and unloaded on
successful landing at a known base. No transfer time, energy, queue, bandwidth
or feedback into the physical controller is introduced. Complete-inspection
timestamps remain the acquisition-completion times used by the executor.
"""
from __future__ import annotations

from collections import defaultdict
from math import isfinite
from statistics import mean

EPS = 1e-7


def _finite(value, label, minimum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise ValueError(f"Non-finite or non-numeric {label}: {value}")
    value = float(value)
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} below {minimum}: {value}")
    return value


def _age_average(initial_timestamp, observations, start, end):
    """Integrate piecewise-linear receiver age, including carry-in updates.

    This integrator deliberately shares no implementation with the independent
    service/energy auditor. An observation is (arrival time, generation stamp).
    """
    ordered = sorted(observations)
    latest = initial_timestamp
    for arrival, stamp in ordered:
        if arrival <= start:
            latest = max(latest, stamp)
        else:
            break
    initial = latest
    left, area = start, 0.0
    for arrival, stamp in ordered:
        if arrival <= start:
            continue
        if arrival > end:
            break
        duration = arrival-left
        area += duration*(left-latest) + duration*duration/2
        left = arrival
        latest = max(latest, stamp)
    duration = end-left
    area += duration*(left-latest) + duration*duration/2
    return area/(end-start), initial, latest


def _summary(rows, start, end, terminal_time):
    """Counts have explicit cohorts; delivered-count includes warm-up carry-in."""
    acquired = [r for r in rows if start <= r["generation_time_s"] <= end]
    deliveries = [r for r in rows if r["delivery_time_s"] is not None
                  and start <= r["delivery_time_s"] <= end]
    by_end = lambda r: r["delivery_time_s"] is not None and r["delivery_time_s"] <= end
    delays = [r["delivery_time_s"]-r["generation_time_s"] for r in deliveries]
    return {
        "collection_complete_count": len(acquired),
        "delivered_count": len(deliveries),
        "eval_cohort_delivered_by_end": sum(by_end(r) for r in acquired),
        "eval_cohort_undelivered_at_end": sum(not by_end(r) for r in acquired),
        "undelivered_at_eval_end": sum(r["generation_time_s"] <= end and not by_end(r) for r in rows),
        "mean_delivery_delay_s": mean(delays) if delays else None,
        "lost_inspections": sum(r["status"] == "lost" for r in rows),
        "pending_inspections_at_end": sum(r["status"] == "pending" for r in rows),
        "total_acquisition_completions": len(rows),
        "total_delivered_by_terminal": sum(r["delivery_time_s"] is not None
                                             and r["delivery_time_s"] <= terminal_time for r in rows),
    }


def evaluate_delivery(scenario: dict, result: dict, mode: str = "base") -> dict:
    """Return ledgers and collection/platform age from one executor result.

    ``base`` means zero-duration unloading on successful landing at any known
    base, followed by immediate synchronization to a common platform. ``instant``
    is the idealized reference in which each accepted step is immediately
    available. Neither mode changes tasks, releases, aircraft actions or energy.

    Step events are assigned to generation 1 until the first corresponding
    inspection_completed event, to generation 2 thereafter, and so forth. This
    matches the executor, which increments its completed-generation counter
    immediately before emitting inspection_completed.
    """
    if mode not in {"base", "instant"}:
        raise ValueError("Delivery mode must be 'base' or 'instant'")
    clock = scenario["clock"]
    start = _finite(clock["evaluation_start_s"], "evaluation start", 0)
    end = _finite(clock["evaluation_end_s"], "evaluation end", 0)
    terminal_time = _finite(result["terminal"]["time_s"], "terminal time", 0)
    if end <= start or terminal_time < end-EPS:
        raise ValueError("The complete evaluation interval must be present in the trace")
    tasks = {row["id"]: row for row in scenario["tasks"]}
    aircraft = {row["id"] for row in scenario["fleet"]}
    bases = {row["id"] for row in scenario["bases"]}
    if len(tasks) != len(scenario["tasks"]) or len(aircraft) != len(scenario["fleet"]):
        raise ValueError("Duplicate task or aircraft identifiers")
    required = {}
    initial_timestamps = {}
    for tid, task in tasks.items():
        required[tid] = len(scenario["templates"][task["template_id"]])
        if not required[tid]:
            raise ValueError(f"Empty acquisition procedure for {tid}")
        initial_timestamps[tid] = -_finite(task.get("initial_age_s", 0), f"initial age {tid}", 0)

    next_generation = {tid: 1 for tid in tasks}
    generation_steps = defaultdict(list)
    steps, inspections = [], []
    carrying = {uid: [] for uid in aircraft}
    failed = set()
    sortie_counts = defaultdict(int)
    active_sorties = {}
    previous_time = -EPS

    def lose_payload(uid, time):
        for index in carrying[uid]:
            step = steps[index]
            if step["status"] == "pending":
                step["status"] = "lost"
                step["lost_time_s"] = time
        carrying[uid].clear()
        failed.add(uid)
        active_sorties.pop(uid, None)

    for event in result["events"]:
        time = _finite(event["time_s"], "event time", 0)
        if time < previous_time-EPS or time > terminal_time+EPS:
            raise ValueError("Event times reverse or extend beyond trace termination")
        previous_time = time
        kind = event["type"]
        if kind not in {"sortie_started", "step_completed", "inspection_completed", "sortie_ended", "aircraft_failed"}:
            continue
        uid = event.get("aircraft_id")
        if uid not in aircraft:
            raise ValueError(f"Unknown acquiring/delivering aircraft: {uid}")
        if kind == "sortie_started":
            if uid in failed or uid in active_sorties:
                raise ValueError(f"Invalid sortie start for {uid}")
            sortie_counts[uid] += 1
            number = event.get("sortie_number", sortie_counts[uid])
            active_sorties[uid] = f"{uid}:{number}"
        elif kind == "step_completed":
            if uid in failed:
                raise ValueError(f"Failed aircraft cannot acquire data: {uid}")
            tid, index = event["task_id"], event["step_index"]
            if tid not in tasks or isinstance(index, bool) or not isinstance(index, int):
                raise ValueError("Unknown task or invalid step index")
            generation = next_generation[tid]
            key = tid, generation
            if index != len(generation_steps[key]) or not 0 <= index < required[tid]:
                raise ValueError(f"Repeated, missing or out-of-order accepted step: {key} step {index}")
            if "generation" in event and event["generation"] != generation:
                raise ValueError("Step generation disagrees with event history")
            row = {
                "task_id": tid, "generation": generation, "step_index": index,
                "acquisition_time_s": time, "aircraft_id": uid,
                "sortie_id": active_sorties.get(uid),
                "status": "delivered" if mode == "instant" else "pending",
                "delivery_time_s": time if mode == "instant" else None,
                "delivery_base_id": None, "lost_time_s": None,
            }
            steps.append(row)
            position = len(steps)-1
            generation_steps[key].append(position)
            if mode == "base":
                carrying[uid].append(position)
        elif kind == "inspection_completed":
            tid = event["task_id"]
            if tid not in tasks:
                raise ValueError(f"Unknown completed task {tid}")
            generation = next_generation[tid]
            if event.get("generation", generation) != generation:
                raise ValueError(f"Inspection generation disagrees for {tid}")
            positions = generation_steps[(tid, generation)]
            if len(positions) != required[tid]:
                raise ValueError("An inspection completed before all required steps were accepted")
            if abs(time-max(steps[index]["acquisition_time_s"] for index in positions)) > EPS:
                raise ValueError("Inspection generation time differs from final step completion")
            inspections.append({
                "task_id": tid, "generation": generation, "generation_time_s": time,
                "delivery_time_s": None, "status": "pending",
                "steps_required": required[tid], "steps_acquired": len(positions),
                "step_keys": [[tid, generation, steps[index]["step_index"]] for index in positions],
                "step_ledger_indices": list(positions),
            })
            next_generation[tid] += 1
        elif kind == "aircraft_failed":
            lose_payload(uid, time)
        elif kind == "sortie_ended":
            outcome = event["outcome"]
            if outcome == "failed":
                lose_payload(uid, time)
            elif outcome == "landed":
                if uid in failed or event.get("base_id") not in bases:
                    raise ValueError("Invalid successful payload landing")
                for index in carrying[uid]:
                    step = steps[index]
                    if step["status"] != "pending":
                        raise ValueError("A payload cannot be unloaded twice")
                    step["status"] = "delivered"
                    step["delivery_time_s"] = time
                    step["delivery_base_id"] = event["base_id"]
                carrying[uid].clear()
                active_sorties.pop(uid, None)
            else:
                raise ValueError(f"Unknown sortie outcome: {outcome}")

    # Resolve complete inspections only after every step's actual fate is known.
    for inspection in inspections:
        rows = [steps[index] for index in inspection["step_ledger_indices"]]
        if all(row["status"] == "delivered" for row in rows):
            delivered = max(row["delivery_time_s"] for row in rows)
            if delivered < inspection["generation_time_s"]-EPS:
                raise ValueError("A complete update cannot be delivered before its generation")
            inspection["status"], inspection["delivery_time_s"] = "delivered", delivered
        elif any(row["status"] == "lost" for row in rows):
            inspection["status"] = "lost"
        inspection["delivered_by_evaluation_end"] = (inspection["delivery_time_s"] is not None
                                                     and inspection["delivery_time_s"] <= end)

    if "completions" in result:
        recorded = sorted((row["task_id"], row["generation"], float(row["time_s"])) for row in result["completions"])
        reconstructed = sorted((row["task_id"], row["generation"], row["generation_time_s"]) for row in inspections)
        if len(recorded) != len(reconstructed) or any(a[:2] != b[:2] or not isfinite(a[2]) or abs(a[2]-b[2]) > EPS
                                                    for a, b in zip(recorded, reconstructed)):
            raise ValueError("Completion ledger and acquisition events disagree")

    updates = sorted(({
        "task_id": row["task_id"], "generation": row["generation"],
        "generation_time_s": row["generation_time_s"], "delivery_time_s": row["delivery_time_s"],
    } for row in inspections if row["delivery_time_s"] is not None),
        key=lambda row: (row["delivery_time_s"], row["task_id"], row["generation_time_s"], row["generation"]))
    latest = dict(initial_timestamps)
    for update in updates:
        tid = update["task_id"]
        before = latest[tid]
        latest[tid] = max(before, update["generation_time_s"])
        update.update(accepted=update["generation_time_s"] > before,
                      previous_timestamp_s=before, latest_timestamp_s=latest[tid])

    per_task = []
    by_task = defaultdict(list)
    for row in inspections:
        by_task[row["task_id"]].append(row)
    for tid, task in tasks.items():
        rows = by_task[tid]
        collection = [(r["generation_time_s"], r["generation_time_s"]) for r in rows]
        platform = [(r["delivery_time_s"], r["generation_time_s"]) for r in rows if r["delivery_time_s"] is not None]
        col_age, col_start, col_end = _age_average(initial_timestamps[tid], collection, start, end)
        plat_age, plat_start, plat_end = _age_average(initial_timestamps[tid], platform, start, end)
        gap = plat_age-col_age
        if gap < -EPS:
            raise ValueError("Platform information cannot be fresher than the acquisition history")
        # Check the pointwise inequality at every jump; between jumps both ages
        # rise at the same rate, so no further interior test is needed.
        known_col = known_platform = initial_timestamps[tid]
        col_at, plat_at = defaultdict(list), defaultdict(list)
        for time, stamp in collection:
            col_at[time].append(stamp)
        for time, stamp in platform:
            plat_at[time].append(stamp)
        for time in sorted(set(col_at) | set(plat_at)):
            known_col = max([known_col, *col_at[time]])
            known_platform = max([known_platform, *plat_at[time]])
            if known_platform > known_col+EPS:
                raise ValueError("Pointwise platform/acquisition age ordering violated")
        per_task.append({
            "task_id": tid, "position_id": task["position_id"], "template_id": task["template_id"],
            "interval_s": task.get("interval_s"), "initial_timestamp_s": initial_timestamps[tid],
            "collection_mean_aoi_s": col_age, "platform_mean_aoi_s": plat_age,
            "mean_aoi_gap_s": max(0., gap),
            "collection_timestamp_at_evaluation_start_s": col_start,
            "platform_timestamp_at_evaluation_start_s": plat_start,
            "collection_timestamp_at_evaluation_end_s": col_end,
            "platform_timestamp_at_evaluation_end_s": plat_end,
            **_summary(rows, start, end, terminal_time),
        })

    incomplete = []
    completed_keys = {(r["task_id"], r["generation"]) for r in inspections}
    for key, positions in generation_steps.items():
        if key not in completed_keys and positions:
            incomplete.append({"task_id": key[0], "generation": key[1],
                               "steps_required": required[key[0]], "steps_acquired": len(positions),
                               "step_ledger_indices": list(positions), "acquisition_complete": False})
    return {
        "schema_version": 1, "scenario_id": result.get("scenario_id", scenario.get("scenario_id")), "mode": mode,
        "evaluation_window_s": [start, end], "terminal_time_s": terminal_time,
        "assumptions": {
            "delivery_mode": "base_instant_offload" if mode == "base" else "instant_step_availability",
            "eligible_bases": "all_known_bases", "transfer_delay_s": 0., "transfer_energy_wh": 0.,
            "upload_capacity_constraint": False, "feedback_to_dispatch": False,
            "timestamp": "complete_acquisition", "initial_information": "same_as_collection",
            "failure_unoffloaded": "lost", "end_unoffloaded": "pending",
            "accumulating_variant": "accepted step output held by accepting aircraft; unaccepted image fragments are not tracked",
        },
        "metric_definitions": {
            "delivered_count": "complete updates with delivery in closed evaluation interval; may include acquisition before evaluation",
            "mean_delivery_delay_s": "mean d-c among complete updates delivered during evaluation; not the mean AoI gap",
            "undelivered_at_eval_end": "all complete acquisitions by evaluation end not yet delivered at that instant",
            "eval_cohort_undelivered_at_end": "complete acquisitions within evaluation not yet delivered at evaluation end",
            "lost_inspections": "complete acquisitions with at least one permanently lost accepted step by trace termination",
            "pending_inspections_at_end": "complete acquisitions not delivered and without known permanent step loss at trace termination",
        },
        "metrics": {
            "age_applicable": scenario.get("business", "periodic") == "periodic",
            "collection_mean_aoi_s": mean(row["collection_mean_aoi_s"] for row in per_task),
            "platform_mean_aoi_s": mean(row["platform_mean_aoi_s"] for row in per_task),
            "mean_aoi_gap_s": mean(row["mean_aoi_gap_s"] for row in per_task),
            **_summary(inspections, start, end, terminal_time),
        },
        "per_task": per_task, "step_ledger": steps, "inspection_ledger": inspections,
        "incomplete_inspections": incomplete, "platform_updates": updates,
        "checks": {"pointwise_platform_age_not_lower": True,
                   "physical_trace_mutated": False, "completion_event_identity_checked": "completions" in result},
    }
