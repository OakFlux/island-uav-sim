"""Meaningful checks of the portable core, saved-run audit and paper reference."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import tempfile
import unittest

from island_uav.runner import (
    DATA_DIR, audit_saved_run, build_scenario, default_config, load_config, run_simulation,
)
import island_sim


class CoreTests(unittest.TestCase):
    def test_default_run_and_independent_saved_audit(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "run"
            result = run_simulation(default_config(), output)
            saved = audit_saved_run(output)
            self.assertEqual(saved, result["audit"])
            self.assertEqual(saved["status"], "pass")
            self.assertGreater(saved["metrics"]["effective_completions"], 0)
            initial = result["scenario"].positions[result["scenario"].base_id]
            positions = {member.aircraft_id: initial for member in result["scenario"].fleet}
            moved = False
            for event in result["events"]:
                aircraft = event.get("aircraft_id")
                if event["kind"] == "energy_interval":
                    position = tuple(event["position_xy"])
                    distance = math.dist(positions[aircraft], position)
                    self.assertAlmostEqual(distance, event["distance_m"], places=5)
                    positions[aircraft] = position
                    moved |= distance > 0
            self.assertTrue(moved)

    def test_paper_reference(self):
        reference = json.loads((DATA_DIR / "reference.json").read_text())
        result = run_simulation(reference["config"])
        report = result["audit"]
        self.assertEqual(report["status"], "pass")
        for path, expected in reference["checks"].items():
            actual = report
            for part in path.split("."):
                actual = actual[part]
            if isinstance(expected, float):
                self.assertTrue(math.isclose(actual, expected, rel_tol=1e-10, abs_tol=1e-8), path)
            else:
                self.assertEqual(actual, expected, path)

    def test_core_source_matches_frozen_provenance(self):
        manifest = json.loads((DATA_DIR / "provenance.json").read_text())
        source = Path(island_sim.__file__).parent
        for name, expected in manifest["core_sha256"].items():
            self.assertEqual(hashlib.sha256((source / name).read_bytes()).hexdigest(), expected, name)

    def test_historical_wind_is_a_separate_input(self):
        scenario = build_scenario({"weather": "historical"})
        self.assertEqual(len(scenario.weather), 7)
        self.assertAlmostEqual(scenario.weather[0].speed_mps, 6.38, places=6)
        self.assertEqual(scenario.weather[0].start_s, 0)
        self.assertEqual(scenario.weather[-1].end_s, 7 * 3600)

    def test_invalid_configuration(self):
        for config in ({"fleet_size": True}, {"fleet_size": 2.0}, {"fleet_size": []},
                       {"duration_hours": float("nan")}, {"unknown": 1},
                       {"weather": "historical", "duration_hours": 72, "warmup_hours": 1}):
            with self.assertRaises(ValueError):
                load_config(config)


if __name__ == "__main__":
    unittest.main()
