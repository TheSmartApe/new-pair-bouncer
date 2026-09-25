"""Replays a data/recordings/ fixture through the dashboard, at no credit cost. Good for repeat filming."""
import asyncio
from pathlib import Path

from rich.live import Live

from core.recorder import read_all

from . import dashboard


async def replay_file(path: str, speed: float = 1.0):
    fixture = Path(path)
    console = dashboard.make_console()
    state = dashboard.DashboardState(strategy_name=fixture.stem, mode="replay", plan_note="replaying a recorded session")
    events = read_all(fixture.name, fixture.parent)
    with Live(dashboard.render(state), console=console, refresh_per_second=4, screen=False) as live:
        last_t = 0
        for rec in events:
            gap = (rec["t"] - last_t) / 1000 / max(speed, 0.01)
            if gap > 0:
                await asyncio.sleep(min(gap, 5))
            last_t = rec["t"]
            if rec["ev"] == "scan":
                data = rec["data"]
                state.scan_count += 1
                state.last_scan_note = data["note"]
                state.last_candidates = len(data["candidates"])
                state.last_checks = data["evaluations"]
                state.last_passed = sum(1 for e in data["evaluations"] if e.get("passed"))
                for note in data.get("locked", []):
                    if note not in state.locked:
                        state.locked.append(note)
            live.update(dashboard.render(state))
