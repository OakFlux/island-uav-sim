"""Directional, piecewise-constant wind kinematics (SI units)."""
from math import hypot, sqrt

EPS = 1e-8


def ground_speed(profile, start, end, wind):
    dx, dy = end[0]-start[0], end[1]-start[1]
    distance = hypot(dx, dy)
    if distance <= EPS:
        return 0.0
    east, north = wind
    if hypot(east, north) > profile['max_flight_wind_mps'] + EPS:
        return None
    along = (east*dx+north*dy)/distance
    cross = (east*dy-north*dx)/distance
    air = profile['max_airspeed_mps']
    if abs(cross) > air + EPS:
        return None
    speed = min(profile['max_groundspeed_mps'], along+sqrt(max(0, air*air-cross*cross)))
    return speed if speed >= profile['min_groundspeed_mps']-EPS else None


def zone_at(point, centers):
    return min(centers, key=lambda key: ((point[0]-centers[key][0])**2 +
                                       (point[1]-centers[key][1])**2, key))


def split_leg(start, end, centers):
    """Exact line/Voronoi intersections; no native grid geometry is implied."""
    dx, dy = end[0]-start[0], end[1]-start[1]
    if hypot(dx, dy) <= EPS:
        return []
    cuts = [0.0, 1.0]
    values = list(centers.values())
    for index, a in enumerate(values):
        for b in values[index+1:]:
            nx, ny = b[0]-a[0], b[1]-a[1]
            denominator = 2*(dx*nx+dy*ny)
            if abs(denominator) <= EPS:
                continue
            numerator = (b[0]**2+b[1]**2-a[0]**2-a[1]**2 -
                         2*(start[0]*nx+start[1]*ny))
            ratio = numerator/denominator
            if EPS < ratio < 1-EPS:
                cuts.append(ratio)
    cuts = sorted(set(round(x, 13) for x in cuts))
    pieces = []
    for left, right in zip(cuts, cuts[1:]):
        a = (start[0]+left*dx, start[1]+left*dy)
        b = (start[0]+right*dx, start[1]+right*dy)
        midpoint = ((a[0]+b[0])/2, (a[1]+b[1])/2)
        z = zone_at(midpoint, centers)
        if pieces and pieces[-1][2] == z:
            pieces[-1] = (pieces[-1][0], b, z)
        else:
            pieces.append((a, b, z))
    return pieces


def overdue_integral(last, start, end, tau):
    return (max(0, end-last-tau)**2-max(0, start-last-tau)**2)/(2*tau)
