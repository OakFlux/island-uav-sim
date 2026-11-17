"""Task-level multi-base UAV inspection simulation.

All inputs and outputs are JSON-compatible dictionaries. The executor alone
advances actual weather; dispatch estimates use the current regional field.
"""
from .engine import simulate, Simulation
from .weather import build_reliability

__all__ = ["simulate", "Simulation", "build_reliability"]
