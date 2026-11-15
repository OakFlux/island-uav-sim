"""Playback must remain consistent with the execution ledger and its audit."""
import copy
import unittest
from island_uav.runner import run_simulation
from island_uav.replay import make_replay, utm_to_lonlat
from island_uav.server import validate_options


class ReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result=run_simulation({'duration_hours':1})
        cls.before=copy.deepcopy(cls.result['events'])
        cls.replay=make_replay(cls.result)

    def test_projection_preserves_ledger_and_matches_consumption(self):
        self.assertEqual(self.before,self.result['events'])
        self.assertAlmostEqual(self.replay['frames'][-1]['energy_wh'],
            sum(e['consumed_wh'] for e in self.before if e['kind']=='energy_interval'),places=3)
        self.assertEqual(self.replay['frames'][-1]['completed'],
            sum(e['kind']=='task_completed' for e in self.before))
        self.assertTrue(all(a['energy_wh']>=-1e-4 for f in self.replay['frames'] for a in f['aircraft']))

    def test_motion_matches_recorded_interval_and_age_resets_only_on_completion(self):
        by_id={}
        checked=0
        for event in self.before:
            key=event.get('aircraft_id')
            if not key or 'position_xy' not in event:
                continue
            start=by_id.get(key,event['position_xy'])
            if event['kind']=='energy_interval' and event['phase']=='transit':
                a,b=event['start_s'],event['end_s']
                for frame in self.replay['frames']:
                    if a+1e-5<frame['t']<b-1e-5:
                        f=(frame['t']-a)/(b-a)
                        xy=[start[i]+f*(event['position_xy'][i]-start[i]) for i in range(2)]
                        lon,lat=utm_to_lonlat(*xy)
                        shown=next(s for s in frame['aircraft'] if s['id']==key)
                        self.assertAlmostEqual(shown['lon'],lon,places=7)
                        self.assertAlmostEqual(shown['lat'],lat,places=7)
                        checked+=1
                        break
            by_id[key]=event['position_xy']
        self.assertGreater(checked,5)
        frame=self.replay['frames'][-1]
        last={s.task_stream_id:-s.initial_age_s for s in self.result['scenario'].periodic_streams}
        for e in self.before:
            if e['kind']=='task_completed':last[e['stream_id']]=e['time_s']
        expected=sum(frame['t']-t for t in last.values())/len(last)/3600
        self.assertAlmostEqual(frame['age_h'],expected,places=4)

    def test_api_presets_reject_unknown_or_unbounded_options(self):
        for options in [{'output':'/tmp/something'},{'fleet_size':True},{'fleet_size':10000},{'duration_hours':float('nan')}]:
            with self.assertRaises(ValueError):validate_options(options)
        self.assertEqual(validate_options({'duration_hours':24})['duration_hours'],24)


if __name__=='__main__':unittest.main()
