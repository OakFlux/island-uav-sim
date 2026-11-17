"""Scientific and portability checks without repeating the full experiment."""
from __future__ import annotations
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import unittest
import island_sim_v5
from island_sim_v5.physics import ground_speed, split_leg
from island_uav.core.delivery import evaluate_delivery
from island_uav.core.wind_frames import grid_wind_components, rotate_weather_to_grid
from island_uav.runner import DATA, audit_saved_run, build_scenario, load_config, run_simulation
from island_uav.reproduction import reference_cases, reference_config, run_reference


class CoreTests(unittest.TestCase):
    def test_invalid_configuration(self):
        invalid=({"fleet_size":True},{"fleet_size":12.0},{"fleet_size":[]},
                 {"duration_hours":float("nan")},{"unknown":1},{"network_id":"legacy_24"},
                 {"weather":"historical","duration_hours":72,"warmup_hours":24,"recovery_hours":2},
                 {"weather":"historical","weather_arrangement":"fragmented"},
                 {"targets_per_sortie":3},{"historical_start_hour":-1})
        for cfg in invalid:
            with self.subTest(config=cfg),self.assertRaises(ValueError):load_config(cfg)

    def test_network_capacity_and_heterogeneity(self):
        for network,fleets,points in (("legacy_24",(2,4,8),24),("pingtan_48",(6,12,24),48),
                                     ("pingtan_96",(6,12,24),96),("pingtan_192",(6,12,24),192)):
            for size in fleets:
                scene=build_scenario(dict(network_id=network,fleet_size=size))
                self.assertEqual(len(scene["tasks"]),points)
                self.assertEqual(len(scene["fleet"]),size)
                for base in scene["bases"]:
                    assigned=[m for m in scene["fleet"] if m["home_base_id"]==base["id"]]
                    self.assertEqual(len(assigned),size//len(scene["bases"]))
                    self.assertEqual({m["profile_id"] for m in assigned},{"LGT","GEN"})
                if network=="legacy_24":self.assertEqual(len(scene["allowed_legs"]),546)
                else:self.assertNotIn("allowed_legs",scene)

    def test_frozen_physical_core(self):
        expected={"__init__.py":"9ff8ac364ab3433d6442ace1ffbf0a1430db32518fe06d00fd770f2dda90843c",
                  "engine.py":"3ed55d6e6b876f7600345a30cb98e353a17750a6f763c7ced32dabdb95e29c6e",
                  "physics.py":"0f1cd8532be6437100fd93dc330f6f346047e98afcbb34c1cfc7bea7fc2a94cd",
                  "weather.py":"4acce6afc0f2375204add2451255a0bc8bd99255b9b2984733c713d3404d66d7"}
        root=Path(island_sim_v5.__file__).parent
        for name,digest in expected.items():
            self.assertEqual(hashlib.sha256((root/name).read_bytes()).hexdigest(),digest,name)

    def test_historical_adapter_matches_archived_inputs(self):
        for case in ("compact","network"):
            assembled=build_scenario(reference_config(case))
            archived=json.loads((DATA/f"references/{case}_input.json").read_text())["scenario"]
            for key in ("fleet","tasks","bases","weather","clock","allowed_legs"):
                self.assertEqual(assembled.get(key),archived.get(key),(case,key))

    def test_wind_rotation_and_no_double_rotation(self):
        x,y=grid_wind_components(0,5,90)
        self.assertAlmostEqual(x,-5);self.assertAlmostEqual(y,0)
        scene=build_scenario(dict(weather="historical",duration_hours=2))
        frames=json.loads((DATA/"weather/wind_coordinate_frames.json").read_text())
        with self.assertRaises(ValueError):
            rotate_weather_to_grid(scene["weather"],scene["weather_zones"],frames,"pingtan_96")
        raw=json.loads((DATA/"weather/ifs_20250404.json").read_text())["rows"][0]
        projected=scene["weather"][raw["zone_id"]][0]
        self.assertAlmostEqual(math.hypot(raw["east_mps"],raw["north_mps"]),
                               math.hypot(projected["east_mps"],projected["north_mps"]),places=12)

    def test_directional_flight_and_spatial_split(self):
        profile=dict(max_flight_wind_mps=20,max_airspeed_mps=10,max_groundspeed_mps=30,min_groundspeed_mps=1)
        self.assertEqual(ground_speed(profile,(0,0),(100,0),(3,0)),13)
        self.assertEqual(ground_speed(profile,(100,0),(0,0),(3,0)),7)
        self.assertAlmostEqual(ground_speed(profile,(0,0),(100,0),(0,6)),8)
        self.assertEqual(split_leg((0,0),(10,0),{"west":(0,0),"east":(10,0)}),
                         [((0.,0.),(5.,0.),"west"),((5.,0.),(10.,0.),"east")])

    def test_cross_aircraft_delivery_age(self):
        scene={"clock":{"evaluation_start_s":0,"evaluation_end_s":100},
               "tasks":[{"id":"T","position_id":"T","template_id":"M","initial_age_s":0}],
               "fleet":[{"id":"A"},{"id":"B"}],"bases":[{"id":"B1"},{"id":"B2"}],
               "templates":{"M":[{},{}]}}
        events=[{"type":"step_completed","time_s":10,"aircraft_id":"A","task_id":"T","step_index":0},
                {"type":"sortie_ended","time_s":20,"aircraft_id":"A","outcome":"landed","base_id":"B1"},
                {"type":"step_completed","time_s":30,"aircraft_id":"B","task_id":"T","step_index":1},
                {"type":"inspection_completed","time_s":30,"aircraft_id":"B","task_id":"T","generation":1},
                {"type":"sortie_ended","time_s":60,"aircraft_id":"B","outcome":"landed","base_id":"B2"}]
        result={"terminal":{"time_s":100},"events":events};before=copy.deepcopy(result)
        delivered=evaluate_delivery(scene,result)
        self.assertEqual(result,before)
        record=delivered["inspection_ledger"][0]
        self.assertEqual(record["generation_time_s"],30)
        self.assertEqual(record["delivery_time_s"],60)
        self.assertEqual(delivered["metrics"]["collection_mean_aoi_s"],29)
        self.assertEqual(delivered["metrics"]["platform_mean_aoi_s"],38)
        self.assertEqual(evaluate_delivery(scene,result,"instant")["metrics"]["mean_aoi_gap_s"],0)

    def test_saved_audit_detects_tampering(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)/"run";result=run_simulation(dict(duration_hours=2),folder)
            self.assertEqual(result["audit"]["status"],"pass")
            self.assertEqual(audit_saved_run(folder)["status"],"pass")
            self.assertGreater(result["physical"]["metrics"]["complete_inspections"],0)
            self.assertGreaterEqual(result["delivery"]["metrics"]["mean_aoi_gap_s"],0)
            self.assertTrue(any(e["type"]=="phase_started" and e["phase"]=="transit" for e in result["events"]))
            path=folder/"physical.json";edited=json.loads(path.read_text())
            edited["metrics"]["operational_energy_wh"]+=10;path.write_text(json.dumps(edited))
            self.assertEqual(audit_saved_run(folder)["status"],"fail")
            with self.assertRaises(FileExistsError):run_simulation(dict(duration_hours=2),folder)

    @unittest.skipUnless(os.environ.get("ISLAND_UAV_REFERENCE_TESTS")=="1","Opt-in historical replay (~20 s); island-uav reproduce performs the same comparison.")
    def test_selected_references(self):
        for case in reference_cases():
            self.assertEqual(run_reference(case)["reference_comparison"]["status"],"pass")


if __name__=="__main__":unittest.main()
