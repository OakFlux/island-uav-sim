"""Current-field lookup and censored development-period opening spells."""
from bisect import bisect_right
from math import hypot, isfinite


class Weather:
    def __init__(self, weather, centers, end_s):
        self.series = {}
        self.starts = {}
        for zone in centers:
            if zone not in weather:
                raise ValueError(f'Missing weather zone: {zone}')
            rows = sorted(weather[zone], key=lambda r: r['start_s'])
            for row in rows:
                if any(not isinstance(row[k],(int,float)) or not isfinite(row[k])
                       for k in ('start_s','end_s','east_mps','north_mps')):
                    raise ValueError(f'Non-finite weather value: {zone}')
            if not rows or rows[0]['start_s'] > 0 or rows[-1]['end_s'] < end_s:
                raise ValueError(f'Weather does not cover simulation: {zone}')
            for left, right in zip(rows, rows[1:]):
                if abs(left['end_s']-right['start_s']) > 1e-7:
                    raise ValueError(f'Non-contiguous weather: {zone}')
            if any(r['end_s'] <= r['start_s'] for r in rows):
                raise ValueError('Weather intervals must have positive duration')
            self.series[zone] = rows
            self.starts[zone] = [r['start_s'] for r in rows]

    def wind(self, zone, time_s):
        index = bisect_right(self.starts[zone], time_s+1e-9)-1
        row = self.series[zone][index]
        return row['east_mps'], row['north_mps']

    def open_age(self, zone, limit, time_s):
        rows = self.series[zone]
        index = bisect_right(self.starts[zone], time_s+1e-9)-1
        if hypot(rows[index]['east_mps'], rows[index]['north_mps']) > limit+1e-8:
            return 0.0
        start = max(0.0, rows[index]['start_s'])
        # Read only observed intervals at/before now, never future rows.
        while index > 0:
            index -= 1
            if hypot(rows[index]['east_mps'], rows[index]['north_mps']) > limit+1e-8:
                break
            start = max(0.0, rows[index]['start_s'])
        return time_s-start


def build_reliability(weather, templates):
    """Only complete, uncensored spells; input must be development years.

    Gaps and the sequence's first/last open spell are censored. Callers record
    product/year provenance; no evaluation-year fitting occurs in the engine.
    """
    limits = sorted({float(step['wind_limit_mps']) for steps in templates.values() for step in steps})
    answer = {}
    for zone, rows in weather.items():
        answer[zone] = {}
        rows = sorted(rows, key=lambda r: r['start_s'])
        for limit in limits:
            complete, opened, known_start, previous_end = [], None, False, None
            for row in rows:
                if previous_end is None or abs(previous_end-row['start_s']) > 1e-7:
                    opened, known_start = None, False
                is_open = hypot(row['east_mps'], row['north_mps']) <= limit+1e-8
                if is_open:
                    if opened is None:
                        opened = row['start_s']
                else:
                    if opened is not None and known_start:
                        complete.append(row['start_s']-opened)
                    opened, known_start = None, True
                previous_end = row['end_s']
            answer[zone][str(limit)] = sorted(complete)
    return answer
