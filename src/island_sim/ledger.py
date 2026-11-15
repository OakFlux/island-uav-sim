"""Append-only, ordered JSON Lines event ledger."""
from __future__ import annotations

import json
from math import isfinite
from typing import Any, IO, Iterator


class EventLedger:
    def __init__(self, destination: IO[str]) -> None:
        self.destination = destination
        self.sequence = 0
        self.last_time_s = float("-inf")

    def emit(self, time_s: float, kind: str, **fields: Any) -> None:
        if not isfinite(time_s) or time_s < self.last_time_s:
            raise ValueError(f"Out-of-order event {kind!r}: {time_s} after {self.last_time_s}")
        event = {"time_s": time_s, "kind": kind, "sequence": self.sequence, **fields}
        self.destination.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
        self.sequence += 1
        self.last_time_s = time_s


def read_events(source: IO[str]) -> Iterator[dict[str, Any]]:
    for line_number, line in enumerate(source, start=1):
        event = json.loads(line)
        if not isinstance(event, dict):
            raise ValueError(f"Ledger line {line_number} is not an event object.")
        yield event
