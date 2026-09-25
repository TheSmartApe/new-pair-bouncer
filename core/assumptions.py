"""Loads and saves assumptions.yaml, the paper-trading assumptions shown in the UI's Assumptions panel."""
from pathlib import Path

import yaml

DEFAULTS = {
    "slippage_bps": 30,
    "fee_bps": 25,
    "starting_cash": 1000,
    "max_position_pct": 0.2,
    "cooldown_s": 30,
    "latency_note": "Decisions are made on polled or streamed data, not a live order book. Real fills would differ.",
}


def load(path: str | Path = "assumptions.yaml") -> dict:
    """Reads assumptions.yaml, filling in any missing keys with defaults."""
    path = Path(path)
    values = dict(DEFAULTS)
    if path.exists():
        values.update(yaml.safe_load(path.read_text()) or {})
    return values


def save(values: dict, path: str | Path = "assumptions.yaml"):
    """Writes assumptions.yaml (only the known keys, in a stable order)."""
    path = Path(path)
    ordered = {k: values.get(k, DEFAULTS[k]) for k in DEFAULTS}
    path.write_text(yaml.safe_dump(ordered, sort_keys=False))
