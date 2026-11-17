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


def make_replay(result: dict, sample_seconds: float = 30) -> dict:
    """Project genuine V5 phases and information histories without altering execution.

    Motion and energy interpolate within one recorded phase only. Aircraft tracks
    retain every actual phase boundary; sampled metric frames are independent.
    """
    scenario, physical, delivery = result['scenario'], result['physical'], result['delivery']
    events = physical['events']
    stop = float(physical['terminal']['time_s'])
    positions = scenario['positions']
    xy = {key: (row['x_m'], row['y_m']) for key, row in positions.items()}
    def geo(point):
        lon, lat = utm_to_lonlat(*point)
        return [round(lon, 8), round(lat, 8)]
    terminals = {row['id']: row for row in physical['terminal']['aircraft']}
    tracks, aircraft = {}, []
    for member in scenario['fleet']:
        uid = member['id']; profile = scenario['profiles'][member['profile_id']]
        aircraft.append({'id': uid, 'profile_id': member['profile_id'], 'capacity_wh': profile['battery_usable_wh'],
                         'capabilities': profile['capabilities'], 'home_base_id': member['home_base_id']})
        phases = [e for e in events if e['type'] == 'phase_started' and e.get('aircraft_id') == uid]
        by_time = {float(e['time_s']): e for e in phases}
        end = terminals[uid]
        by_time[stop] = {'time_s': stop, 'position': end['position'], 'energy_wh': end['energy_wh'],
                         'phase': end['phase'], 'base_id': end['current_base_id'], 'task_id': None,
                         'power_w': 0, 'charge_battery_w': 0}
        ordered = [by_time[t] for t in sorted(by_time)]
        rows = []; used_before = 0.
        for index, event in enumerate(ordered):
            nxt = ordered[min(index+1, len(ordered)-1)]
            dt = nxt['time_s']-event['time_s']
            moving = event['phase'] in {'transit', 'return'}
            pos = geo(event['position']); endpos = geo(nxt['position']) if moving else pos
            energy_end = event['energy_wh'] + (event.get('charge_battery_w', 0)-event.get('power_w', 0))*dt/3600
            rows.append({'t': event['time_s'], 'end_t': nxt['time_s'], 'lon': pos[0], 'lat': pos[1],
                         'end_lon': endpos[0], 'end_lat': endpos[1], 'phase': event['phase'],
                         'energy_wh': event['energy_wh'], 'end_energy_wh': max(0, energy_end),
                         'task_id': event.get('task_id'), 'base_id': event.get('base_id'),
                         'power_w': event.get('power_w', 0), 'consumed_wh_start': used_before})
            used_before += event.get('power_w', 0)*dt/3600
        tracks[uid] = rows
    starts = {uid: [r['t'] for r in rows] for uid, rows in tracks.items()}
    def state_at(uid, t):
        row = tracks[uid][max(0, bisect_right(starts[uid], t)-1)]
        f = min(1, max(0, (t-row['t'])/(row['end_t']-row['t']))) if row['end_t'] > row['t'] else 0
        return dict(id=uid, lon=row['lon']+f*(row['end_lon']-row['lon']), lat=row['lat']+f*(row['end_lat']-row['lat']),
                    energy_wh=row['energy_wh']+f*(row['end_energy_wh']-row['energy_wh']),
                    phase=row['phase'], task_id=row['task_id'], base_id=row['base_id'])
    task_ids = {task['id']: task for task in scenario['tasks']}
    stamps_c = {tid: -task.get('initial_age_s', 0) for tid, task in task_ids.items()}
    stamps_p = dict(stamps_c)
    acquisitions = sorted(physical['completions'], key=lambda e: e['time_s'])
    receipts = sorted(delivery['platform_updates'], key=lambda e: e['delivery_time_s'])
    step = max(float(sample_seconds), stop/1800)
    times = sorted({0., stop, float(scenario['clock']['evaluation_start_s']), float(scenario['clock']['evaluation_end_s']),
                    *[min(stop, i*step) for i in range(int(stop/step)+1)],
                    *[r['start_s'] for series in scenario['weather'].values() for r in series]})
    ci = pi = 0; frames = []
    # Exact cumulative phase power: every interval comes from the physical events.
    consumption = []
    for uid, rows in tracks.items():
        phases = {float(e['time_s']): e for e in events if e['type'] == 'phase_started' and e.get('aircraft_id') == uid}
        for row in rows:
            power = phases.get(row['t'], {}).get('power_w', 0)
            consumption.append((row['t'], row['end_t'], power))
    power_events = sorted([(a, power) for a,b,power in consumption] + [(b, -power) for a,b,power in consumption])
    power_i = 0; total_power = 0.; energy_integral = 0.; last_power_t = 0.
    for t in times:
        while power_i < len(power_events) and power_events[power_i][0] <= t:
            at, delta = power_events[power_i]; energy_integral += (at-last_power_t)*total_power/3600
            last_power_t = at; total_power += delta; power_i += 1
        while ci < len(acquisitions) and acquisitions[ci]['time_s'] <= t+1e-8:
            event = acquisitions[ci]; stamps_c[event['task_id']] = event['time_s']; ci += 1
        while pi < len(receipts) and receipts[pi]['delivery_time_s'] <= t+1e-8:
            event = receipts[pi]; tid = event['task_id']; stamps_p[tid] = max(stamps_p[tid], event['generation_time_s']); pi += 1
        current = [state_at(uid, t) for uid in tracks]
        winds = []
        for region in scenario['weather_zones']:
            series = scenario['weather'][region['id']]
            at = min(len(series)-1, max(0, bisect_right([r['start_s'] for r in series], t)-1)); wind = series[at]
            lon, lat = geo((region['x_m'], region['y_m']))
            winds.append({'id': region['id'], 'lon': lon, 'lat': lat, 'east': wind['east_mps'], 'north': wind['north_mps']})
        consumed = energy_integral + (t-last_power_t)*total_power/3600
        frames.append({'t': t, 'aircraft': current, 'completed': ci, 'delivered': pi, 'energy_wh': consumed,
                       'age_h': sum(t-stamps_c[tid] for tid in task_ids)/len(task_ids)/3600,
                       'platform_age_h': sum(t-stamps_p[tid] for tid in task_ids)/len(task_ids)/3600,
                       'overdue': sum(max(0, (t-stamps_c[tid])/task['interval_s']-1) for tid, task in task_ids.items())/len(task_ids),
                       'charging': sum(a['phase']=='charging' for a in current), 'queue': sum(a['phase']=='charge_wait' for a in current),
                       'winds': winds, 'wind_east_mps': winds[0]['east'], 'wind_north_mps': winds[0]['north']})
    sites = []
    source_sites = {s['id']: s for s in result.get('metadata', {}).get('sites', [])}
    for task in scenario['tasks']:
        lon, lat = geo(xy[task['position_id']]); source = source_sites.get(task['id'], {})
        sites.append({'id': task['position_id'], 'task_id': task['id'], 'lon': lon, 'lat': lat, 'template': task['template_id'],
                      'interval_s': task['interval_s'], 'initial_age_s': task.get('initial_age_s', 0),
                      'evidence': source.get('evidence', source.get('evidence_class', 'scenario'))})
    bases = []
    for base in scenario['bases']:
        lon, lat = geo(xy[base['position_id']]); bases.append({'id': base['id'], 'lon': lon, 'lat': lat,
                     'ports': base['charge_ports'], 'power_w': base['port_input_w']})
    visible = {'inspection_completed', 'service_interrupted', 'recovery_selected', 'sortie_started', 'sortie_ended', 'aircraft_failed', 'weather_changed'}
    log = []
    for event in events:
        if event['type'] not in visible: continue
        uid, tid = event.get('aircraft_id'), event.get('task_id')
        text = {'inspection_completed': 'Inspection complete', 'service_interrupted': 'Sensing interrupted', 'recovery_selected': 'Recovering',
                'sortie_started': 'Departing', 'sortie_ended': 'Landed' if event.get('outcome')=='landed' else 'Sortie ended',
                'aircraft_failed': 'Aircraft unavailable', 'weather_changed': 'Wind changed'}[event['type']]
        log.append({'t': event['time_s'], 'kind': event['type'], 'aircraft_id': uid, 'site_id': tid,
                    'base_id':event.get('base_id'),'origin_base_id':event.get('origin_base_id'),
                    'cross_base':event['type']=='sortie_ended' and event.get('outcome')=='landed' and event.get('origin_base_id')!=event.get('base_id'),
                    'message': ' · '.join(str(x) for x in [text, uid, tid, event.get('base_id')] if x)})
    bounds_points = sites+bases
    lo, hi = min(s['lon'] for s in bounds_points), max(s['lon'] for s in bounds_points)
    bottom, top = min(s['lat'] for s in bounds_points), max(s['lat'] for s in bounds_points)
    config = result['config']; metadata = dict(result.get('metadata', {}))
    metadata.update({'title': 'Pingtan Island inspection', 'duration_s': stop, 'evaluation_start_s': scenario['clock']['evaluation_start_s'],
                     'evaluation_end_s': scenario['clock']['evaluation_end_s'], 'map_bounds': [lo-.009,bottom-.009,hi+.009,top+.009],
                     'config': config, 'configuration': config, 'weather': config['weather'], 'policy': config['policy'],
                     'wind_label': 'Illustrative synthetic wind' if config['weather']=='synthetic' else 'Historical regional wind',
                     'metrics_note': 'Displayed ages are instantaneous means; energy and completions accumulate from the beginning of this replay. Evaluation-window means are in the downloaded summary.',
                     'sample_seconds': step})
    for key in ('sites', 'bases', 'weather_zones'): metadata.pop(key, None)
    return {'metadata': metadata, 'base': bases[0], 'bases': bases, 'sites': sites, 'aircraft': aircraft,
            'tracks': tracks, 'frames': frames, 'events': log,
            'collection_history': [{'t':e['time_s'],'task_id':e['task_id'],'stamp':e['time_s']} for e in acquisitions],
            'platform_history': [{'t':e['delivery_time_s'],'task_id':e['task_id'],'stamp':e['generation_time_s']} for e in receipts],
            'summary': {'physical': result.get('audit'), 'collection': physical['metrics'], 'delivery': delivery['metrics']}}
