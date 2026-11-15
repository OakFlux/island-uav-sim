"""Read-only event projection for the browser; the scientific engine is unchanged."""
from __future__ import annotations

from bisect import bisect_right
from math import cos, degrees, sin, sqrt, tan


def utm_to_lonlat(x: float, y: float, zone: int = 50) -> tuple[float, float]:
    """Inverse northern-hemisphere WGS84 UTM (Snyder series)."""
    a, e2, k0 = 6378137.0, 0.0066943799901413165, 0.9996
    ep2 = e2 / (1 - e2)
    mu = (y / k0) / (a * (1 - e2 / 4 - 3 * e2**2 / 64 - 5 * e2**3 / 256))
    e1 = (1 - sqrt(1-e2)) / (1 + sqrt(1-e2))
    fp = mu + (3*e1/2 - 27*e1**3/32)*sin(2*mu) + (21*e1**2/16 - 55*e1**4/32)*sin(4*mu) + 151*e1**3/96*sin(6*mu) + 1097*e1**4/512*sin(8*mu)
    c, t = ep2*cos(fp)**2, tan(fp)**2
    n = a / sqrt(1-e2*sin(fp)**2)
    r = a*(1-e2) / (1-e2*sin(fp)**2)**1.5
    d = (x-500000.0)/(n*k0)
    lat = fp - n*tan(fp)/r*(d*d/2 - (5+3*t+10*c-4*c*c-9*ep2)*d**4/24 + (61+90*t+298*c+45*t*t-252*ep2-3*c*c)*d**6/720)
    lon = (d - (1+2*t+c)*d**3/6 + (5-2*c+28*t-3*c*c+8*ep2+24*t*t)*d**5/120)/cos(fp)
    return degrees(lon) + (zone-1)*6-180+3, degrees(lat)


def make_replay(result: dict, sample_seconds: float = 10) -> dict:
    """Interpolate only within recorded energy/motion intervals, never between visits."""
    scenario, events = result['scenario'], result['events']
    metadata = dict(result.get('metadata', {}))
    points = scenario.positions
    geo = {key: utm_to_lonlat(*xy) for key, xy in points.items()}
    duration = float(scenario.recovery_end_s)
    streams = {s.task_stream_id: s for s in scenario.periodic_streams}
    last = {key: -s.initial_age_s for key, s in streams.items()}
    profiles = {m.aircraft_id: scenario.profiles[m.profile_id] for m in scenario.fleet}
    state = {m.aircraft_id: {'position_xy': points[scenario.base_id], 'phase': 'idle',
             'energy_wh': profiles[m.aircraft_id].battery_usable_wh*m.initial_soc_fraction, 'task_id': None}
             for m in scenario.fleet}
    intervals = {key: [] for key in state}
    for e in events:
        if e['kind'] == 'energy_interval' and e.get('end_s', 0) > e.get('start_s', 0):
            intervals[e['aircraft_id']].append(dict(e))
    starts = {key: [e['start_s'] for e in rows] for key, rows in intervals.items()}
    interval_by_sequence = {e['sequence']: e for rows in intervals.values() for e in rows}
    # For an interval ending at p, use the preceding recorded position as its start.
    previous = {key: points[scenario.base_id] for key in state}
    for e in events:
        key = e.get('aircraft_id')
        if key not in state:
            continue
        pos = e.get('position_xy')
        if pos is not None:
            if e['kind'] == 'energy_interval':
                interval_by_sequence[e['sequence']]['_replay_start_xy'] = previous[key]
            previous[key] = pos
    important = {float(e['time_s']) for e in events if e['kind'] in {
        'phase_changed', 'task_completed', 'weather_changed', 'evaluation_closed'}}
    times = sorted({0.0, duration, float(scenario.evaluation_start_s), float(scenario.evaluation_end_s),
                    *important, *[float(i*sample_seconds) for i in range(int(duration/sample_seconds)+1)]})
    event_index, completed, consumed = 0, 0, 0.0
    wind = (0.0, 0.0)
    frames = []
    for t in times:
        if t > duration:
            continue
        while event_index < len(events) and events[event_index]['time_s'] <= t + 1e-8:
            e = events[event_index]; event_index += 1
            key, kind = e.get('aircraft_id'), e['kind']
            if kind == 'weather_changed':
                wind = (e['wind_east_mps'], e['wind_north_mps'])
            if kind == 'task_completed':
                completed += 1
                if e.get('stream_id') in last:
                    last[e['stream_id']] = e['time_s']
            if key not in state:
                continue
            st = state[key]
            if 'position_xy' in e:
                st['position_xy'] = e['position_xy']
            if kind == 'phase_changed':
                st['phase'] = e['phase']; st['energy_wh'] = e['energy_wh']
            if kind == 'task_assigned':
                st['task_id'] = e['task_id']
            if kind in {'task_completed', 'task_interrupted'}:
                st['task_id'] = None
            if kind == 'energy_interval':
                st['energy_wh'] = e['energy_after_wh']; consumed += e['consumed_wh']
        aircraft, partial_consumed = [], 0.0
        for key, st in state.items():
            xy, energy = st['position_xy'], st['energy_wh']
            index = bisect_right(starts[key], t + 1e-8)-1
            if index >= 0:
                interval = intervals[key][index]
                a, b = interval['start_s'], interval['end_s']
                if a <= t < b:
                    f = max(0.0, min(1.0, (t-a)/(b-a)))
                    energy = interval['energy_before_wh'] + f*(interval['energy_after_wh']-interval['energy_before_wh'])
                    partial_consumed += interval['consumed_wh']*f
                    if interval['phase'] in {'transit', 'return'}:
                        start, end = interval['_replay_start_xy'], interval.get('position_xy', xy)
                        xy = [start[i]+f*(end[i]-start[i]) for i in range(2)]
            lon, lat = utm_to_lonlat(*xy)
            aircraft.append({'id': key, 'lon': round(lon,8), 'lat': round(lat,8),
                'energy_wh': round(energy,4), 'phase': st['phase'], 'task_id': st['task_id']})
        ages = [(t-last[key]) for key in streams]
        overdue = [max(0, (t-last[key])/s.target_interval_s-1) for key, s in streams.items()]
        frames.append({'t': round(t,6), 'aircraft': aircraft, 'completed': completed,
            'energy_wh': round(consumed+partial_consumed,4),
            'age_h': round(sum(ages)/len(ages)/3600,5) if ages else 0,
            'overdue': round(sum(overdue)/len(overdue),5) if overdue else 0,
            'wind_east_mps': wind[0], 'wind_north_mps': wind[1],
            'charging': sum(st['phase']=='charging' for st in state.values()),
            'queue': sum(st['phase']=='charge_wait' for st in state.values())})
    site_metadata = {s['id']: s for s in metadata.get('sites', [])}
    sites = []
    for s in scenario.periodic_streams:
        lon, lat = geo[s.asset_id]
        sites.append({'id': s.asset_id, 'lon': lon, 'lat': lat, 'template': s.template_id,
            'interval_s': s.target_interval_s, 'initial_age_s': s.initial_age_s,
            'evidence': site_metadata.get(s.asset_id,{}).get('evidence','scenario')})
    visible = {'task_assigned','task_completed','task_interrupted','unit_interrupted','sortie_started',
               'sortie_finished','queue_entered','queue_left','weather_changed','aircraft_failed'}
    log = []
    for e in events:
        if e['kind'] not in visible:
            continue
        log.append({'t': e['time_s'], 'kind': e['kind'], 'aircraft_id': e.get('aircraft_id'),
            'site_id': e.get('asset_id'), 'task_id': e.get('task_id'),
            'message': e['kind'].replace('_',' ').capitalize() +
                (' · '+e['aircraft_id'] if e.get('aircraft_id') else '') +
                (' · '+e['asset_id'] if e.get('asset_id') else '')})
    lon, lat = geo[scenario.base_id]
    metadata.update({'title':'Pingtan Island inspection', 'duration_s': duration,
        'evaluation_start_s': scenario.evaluation_start_s, 'evaluation_end_s': scenario.evaluation_end_s,
        'policy': result['config'].get('policy','B2'), 'weather': result['config'].get('weather','synthetic'),
        'map_bounds': [119.674,25.443,119.792,25.540],
        'configuration': result['config'], 'config': result['config'], 'sample_seconds': sample_seconds,
        'metrics_note':'Current mean AoI and overdue ratio are across requirements at the displayed time. Counts and energy are cumulative from simulation start; audited evaluation metrics are in summary.'})
    metadata.pop('sites', None);metadata.pop('base',None)
    return {'metadata':metadata, 'base':{'id':scenario.base_id,'lon':lon,'lat':lat}, 'sites':sites,
        'aircraft':[{'id':m.aircraft_id,'profile_id':m.profile_id,
            'capacity_wh':profiles[m.aircraft_id].battery_usable_wh,
            'capabilities':sorted(profiles[m.aircraft_id].capabilities)} for m in scenario.fleet],
        'frames':frames,'events':log,'summary':result['audit']}
