"""Captures a short live scan session to data/recordings/, so it can be replayed and filmed again for free."""
import asyncio
from pathlib import Path

from core.client import CoinGeckoClient
from core.plan import probe_capabilities
from core.recorder import Recorder

from .config import RECORDINGS_DIR
from .scan import enrich_and_screen, fetch_candidates
from .strategy import Strategy


async def record_session(strategy: Strategy, scans: int = 3, interval_s: float = 10.0) -> Path:
    """Runs `scans` real scans and writes every input + check result to a JSONL fixture."""
    client = CoinGeckoClient()
    try:
        caps = await probe_capabilities(client)
        rec = Recorder(demo=strategy.name, tag="scan", fixtures_dir=Path(RECORDINGS_DIR))
        rec.write("caps", caps)
        for i in range(scans):
            candidates, note, locked = await fetch_candidates(client, strategy, caps)
            evaluations, locked2 = await enrich_and_screen(client, strategy, candidates, caps)
            rec.write(
                "scan",
                {
                    "note": note,
                    "candidates": candidates,
                    "evaluations": [e.as_dict() for e in evaluations],
                    "locked": locked + locked2,
                },
            )
            if i < scans - 1:
                await asyncio.sleep(interval_s)
        rec.close()
        return rec.path
    finally:
        await client.close()
