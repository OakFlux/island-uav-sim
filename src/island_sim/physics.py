"""Task-level planar motion under a piecewise-constant wind vector."""
from __future__ import annotations

from math import hypot, sqrt

from .domain import AircraftProfile, Point

WIND_TOLERANCE = 1e-8


def ground_speed(
    profile: AircraftProfile,
    origin_xy: Point,
    destination_xy: Point,
    east_mps: float,
    north_mps: float,
) -> float | None:
    """Return the fastest permitted along-track ground speed, or None."""
    if hypot(east_mps, north_mps) > profile.max_flight_wind_mps + WIND_TOLERANCE:
        return None
    dx = destination_xy[0] - origin_xy[0]
    dy = destination_xy[1] - origin_xy[1]
    distance = hypot(dx, dy)
    if distance == 0:
        return 0.0
    along = (dx * east_mps + dy * north_mps) / distance
    cross = (dx * north_mps - dy * east_mps) / distance
    maximum = profile.max_airspeed_mps
    if abs(cross) > maximum + WIND_TOLERANCE:
        return None
    along_air = sqrt(max(0.0, maximum**2 - cross**2))
    fastest = min(profile.max_groundspeed_mps, along + along_air)
    slowest = max(profile.min_groundspeed_mps, along - along_air)
    if fastest + WIND_TOLERANCE < slowest or fastest <= 0:
        return None
    return fastest


def leg_time(
    profile: AircraftProfile,
    origin_xy: Point,
    destination_xy: Point,
    east_mps: float,
    north_mps: float,
) -> float | None:
    speed = ground_speed(profile, origin_xy, destination_xy, east_mps, north_mps)
    if speed is None:
        return None
    if speed == 0:
        return 0.0
    return hypot(destination_xy[0] - origin_xy[0], destination_xy[1] - origin_xy[1]) / speed
