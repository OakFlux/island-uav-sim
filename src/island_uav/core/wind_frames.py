"""Rotate source true-east/north winds into the projected working axes.

Runtime is standard-library only. Meridian convergence and anchor coordinates
come from the separately prepared, frozen frame table. Physical wind speed is
preserved: projection scale is not multiplied into a wind-speed observation.
"""
from __future__ import annotations

from math import cos, isclose, isfinite, radians, sin

SOURCE_FRAME = "true_east_north"
TARGET_FRAME = "projected_grid_xy"
WORKING_CRS = "EPSG:32650"


def grid_wind_components(true_east_mps: float, true_north_mps: float,
                         meridian_convergence_deg: float) -> tuple[float, float]:
    """Use PROJ's convergence convention, verified by projected true bearings.

    Positive gamma maps a unit true-north vector to (-sin gamma, cos gamma).
    Thus x = u*cos(gamma)-v*sin(gamma), y = u*sin(gamma)+v*cos(gamma).
    """
    if not all(isfinite(value) for value in (true_east_mps, true_north_mps,
                                             meridian_convergence_deg)):
        raise ValueError("Wind components and meridian convergence must be finite")
    angle = radians(meridian_convergence_deg)
    c, s = cos(angle), sin(angle)
    return true_east_mps*c-true_north_mps*s, true_east_mps*s+true_north_mps*c


def rotate_weather_to_grid(weather: dict, zones: list[dict], frames: dict,
                           network_id: str) -> dict:
    """Return grid-axis wind intervals, validating network/anchor identity.

    The caller must bypass this adapter for arbitrary Cartesian controlled
    scenarios. A per-interval frame marker prevents a second rotation of its
    own output. Input dictionaries, physical speed and interval times are kept.
    The existing east_mps/north_mps fields denote working x/y after conversion;
    vector_frame makes that convention explicit in every stored historical run.
    """
    if (frames.get("schema_version") != 1 or frames.get("crs") != WORKING_CRS
            or frames.get("source_frame") != SOURCE_FRAME
            or frames.get("target_frame") != TARGET_FRAME
            or frames.get("scale_applied") is not False):
        raise ValueError("Unsupported wind coordinate-frame specification")
    if network_id in {"pingtan_48", "pingtan_96", "pingtan_192"}:
        group = "pingtan"
    elif network_id == "legacy_24":
        group = "legacy_24"
    else:
        raise ValueError(f"No geographic wind-frame mapping for {network_id}")
    reference = frames["networks"][group]["zones"]
    by_id = {z["id"]: z for z in zones}
    if len(by_id) != len(zones) or set(by_id) != set(reference) or set(weather) != set(reference):
        raise ValueError("Weather zones do not match the frozen wind-frame mapping")
    result = {}
    for zone_id, zone in by_id.items():
        frame = reference[zone_id]
        for axis in ("x", "y"):
            actual, expected = zone[f"{axis}_m"], frame[f"scene_anchor_{axis}_m"]
            if not (isfinite(actual) and isfinite(expected)
                    and isclose(actual, expected, rel_tol=0., abs_tol=1e-6)):
                raise ValueError(f"Wind frame anchor differs for {zone_id}/{axis}")
        intervals = []
        for row in weather[zone_id]:
            if row.get("vector_frame", SOURCE_FRAME) != SOURCE_FRAME:
                raise ValueError(f"Wind is already projected or has an unknown frame: {zone_id}")
            x, y = grid_wind_components(row["east_mps"], row["north_mps"],
                                        frame["meridian_convergence_deg"])
            intervals.append({**row, "east_mps": x, "north_mps": y,
                              "vector_frame": TARGET_FRAME})
        result[zone_id] = intervals
    return result
