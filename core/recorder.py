"""JSONL record/replay, so a live session can be captured once and replayed for free. Each line: {"t", "ev", "data"}."""
import asyncio
import json
import re
import time
from datetime import datetime
from pathlib import Path

FIXTURES_DIR = Path("fixtures")
_SAFE_NAME = re.compile(r"^[a-z0-9_-]+\.jsonl$")


class Recorder:
    """Writes one JSONL fixture file, timestamped relative to when recording started."""

    def __init__(self, demo: str, tag: str, fixtures_dir: Path = FIXTURES_DIR):
        fixtures_dir.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.name = f"{demo}-{tag}-{stamp}.jsonl"
        self.path = fixtures_dir / self.name
        self._fh = self.path.open("w")
        self._t0 = time.perf_counter()

    def write(self, ev: str, data):
        """Appends one event as a JSON line, flushed immediately."""
        self._fh.write(json.dumps({"t": round((time.perf_counter() - self._t0) * 1000), "ev": ev, "data": data}) + "\n")
        self._fh.flush()

    def close(self):
        """Closes the fixture file."""
        self._fh.close()


def fixture_path(name: str, fixtures_dir: Path = FIXTURES_DIR) -> Path:
    """Resolves a fixture name to its path, rejecting anything that isn't a plain filename."""
    if not _SAFE_NAME.match(name):
        raise ValueError("bad fixture name")
    path = fixtures_dir / name
    if not path.exists():
        raise FileNotFoundError(name)
    return path


def read_all(name: str, fixtures_dir: Path = FIXTURES_DIR) -> list[dict]:
    """Reads every event from a fixture file."""
    with fixture_path(name, fixtures_dir).open() as fh:
        return [json.loads(line) for line in fh if line.strip()]


async def replay(name: str, speed: float = 1.0, skip: tuple = (), fixtures_dir: Path = FIXTURES_DIR):
    """Yields (event, data) pairs, sleeping between them to match the original timing at `speed`."""
    last = 0
    for rec in read_all(name, fixtures_dir):
        if rec["ev"] in skip:
            continue
        gap = (rec["t"] - last) / 1000 / max(speed, 0.01)
        if gap > 0:
            await asyncio.sleep(min(gap, 5))
        last = rec["t"]
        yield rec["ev"], rec["data"]


def list_fixtures(demo: str, fixtures_dir: Path = FIXTURES_DIR) -> list[dict]:
    """Every recorded fixture for `demo`, newest first."""
    if not fixtures_dir.exists():
        return []
    return [{"name": p.name, "bytes": p.stat().st_size} for p in sorted(fixtures_dir.glob(f"{demo}-*.jsonl"), reverse=True)]
