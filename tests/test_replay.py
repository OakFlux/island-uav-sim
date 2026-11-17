"""Read-only browser projections must agree with executed V5 event accounts."""
import copy
import unittest

from island_uav.runner import run_simulation
from island_uav.replay import make_replay, utm_to_lonlat
from island_uav.server import validate_options


class ReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = run_simulation({'duration_hours': 6})
        cls.before = copy.deepcopy(cls.result['physical'])
        cls.replay = make_replay(cls.result)

    def test_projection_preserves_execution_and_total_energy(self):
        self.assertEqual(self.before, self.result['physical'])
        self.assertAlmostEqual(self.replay['frames'][-1]['energy_wh'],
                               self.before['energy_ledger']['consumed_wh'], places=6)
        self.assertEqual(self.replay['frames'][-1]['completed'], len(self.before['completions']))
        self.assertEqual(self.replay['frames'][-1]['delivered'],
                         self.result['delivery']['metrics']['total_delivered_by_terminal'])
        self.assertTrue(all(a['energy_wh'] >= -1e-6 for f in self.replay['frames'] for a in f['aircraft']))

    def test_actual_phase_boundaries_and_interpolated_motion(self):
        checked = 0
        for uid, track in self.replay['tracks'].items():
            for row in track:
                if row['phase'] not in {'transit', 'return'} or row['end_t'] <= row['t']:
                    continue
                self.assertNotEqual((row['lon'], row['lat']), (row['end_lon'], row['end_lat']))
                inside = next((f for f in self.replay['frames'] if row['t'] < f['t'] < row['end_t']), None)
                if inside:
                    frac = (inside['t']-row['t'])/(row['end_t']-row['t'])
                    shown = next(a for a in inside['aircraft'] if a['id'] == uid)
                    self.assertAlmostEqual(shown['lon'], row['lon']+frac*(row['end_lon']-row['lon']), places=9)
                    self.assertAlmostEqual(shown['lat'], row['lat']+frac*(row['end_lat']-row['lat']), places=9)
                    checked += 1
        self.assertGreater(checked, 10)

    def test_both_age_boundaries_use_genuine_information_timestamps(self):
        tasks = self.result['scenario']['tasks']
        for frame in self.replay['frames'][::97]:
            stamps = {t['id']: -t['initial_age_s'] for t in tasks}
            for event in self.before['completions']:
                if event['time_s'] <= frame['t']+1e-8:
                    stamps[event['task_id']] = event['time_s']
            expected = sum(frame['t']-stamp for stamp in stamps.values())/len(stamps)/3600
            self.assertAlmostEqual(frame['age_h'], expected, places=9)
            stamps = {t['id']: -t['initial_age_s'] for t in tasks}
            for event in self.result['delivery']['platform_updates']:
                if event['delivery_time_s'] <= frame['t']+1e-8:
                    stamps[event['task_id']] = max(stamps[event['task_id']], event['generation_time_s'])
            expected = sum(frame['t']-stamp for stamp in stamps.values())/len(stamps)/3600
            self.assertAlmostEqual(frame['platform_age_h'], expected, places=9)
            self.assertGreaterEqual(frame['platform_age_h']+1e-9, frame['age_h'])

    def test_terminal_positions_match_execution(self):
        end = {a['id']: a for a in self.before['terminal']['aircraft']}
        for shown in self.replay['frames'][-1]['aircraft']:
            lon, lat = utm_to_lonlat(*end[shown['id']]['position'])
            self.assertAlmostEqual(shown['lon'], lon, places=7)
            self.assertAlmostEqual(shown['lat'], lat, places=7)
            self.assertAlmostEqual(shown['energy_wh'], end[shown['id']]['energy_wh'], places=6)

    def test_replay_bounded_metric_sampling(self):
        # Changing metric sampling does not discard physical phase boundaries.
        sparse = make_replay(self.result, sample_seconds=3600)
        self.assertLess(len(sparse['frames']), len(self.replay['frames']))
        self.assertEqual(sparse['tracks'], self.replay['tracks'])
        self.assertEqual(len(self.replay['sites']), 96)
        self.assertEqual(len(self.replay['bases']), 3)
        self.assertTrue(any(len({e['base_id'] for e in rows}) > 1 for rows in self.replay['tracks'].values()))

    def test_compact_task_ids_are_explicitly_mapped_to_sites(self):
        result = run_simulation({'network_id':'legacy_24','fleet_size':4,'weather':'historical','duration_hours':6,'recovery_policy':'HOME'})
        replay = make_replay(result)
        self.assertTrue(any(s['task_id'] != s['id'] for s in replay['sites']))
        for frame in replay['frames'][::37]:
            stamps = {s['task_id']: -s['initial_age_s'] for s in replay['sites']}
            for event in replay['collection_history']:
                if event['t'] <= frame['t']:
                    stamps[event['task_id']] = max(stamps[event['task_id']], event['stamp'])
            expected = sum(frame['t']-stamp for stamp in stamps.values())/len(stamps)/3600
            self.assertAlmostEqual(frame['age_h'], expected, places=9)
        self.assertGreater(replay['frames'][-1]['completed'], 0)

    def test_api_rejects_unknown_and_unbounded_options(self):
        for options in [{'output':'/tmp/x'}, {'fleet_size': True}, {'fleet_size':10000}, {'duration_hours':float('nan')}]:
            with self.assertRaises(ValueError):
                validate_options(options)
        self.assertEqual(validate_options({'duration_hours':24})['duration_hours'], 24)


if __name__ == '__main__':
    unittest.main()
