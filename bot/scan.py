"""One scan: pull candidates from the strategy's source, screen them tier by tier, log every check."""
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from core.client import CoinGeckoClient, PlanRestrictedError

from . import config
from .candidates import normalize_pool, normalize_token_info
from .strategy import CheckResult, Evaluation, Strategy


@dataclass
class ScanResult:
    ts: float
    strategy_name: str
    source_note: str
    candidates: list[dict] = field(default_factory=list)
    evaluations: list[Evaluation] = field(default_factory=list)
    locked: list[str] = field(default_factory=list)
    credits_used: int = 0

    @property
    def passed(self) -> list[Evaluation]:
        return [e for e in self.evaluations if e.passed]

    def as_dict(self) -> dict:
        return {
            "ts": self.ts,
            "strategy": self.strategy_name,
            "source_note": self.source_note,
            "candidate_count": len(self.candidates),
            "locked": self.locked,
            "credits_used": self.credits_used,
            "evaluations": [e.as_dict() for e in self.evaluations],
        }


async def fetch_candidates(client: CoinGeckoClient, strategy: Strategy, caps: dict) -> tuple[list[dict], str, list[str]]:
    """Pulls raw pool rows for the strategy's configured source, falling back and noting any lock."""
    src = strategy.source
    chain = src.get("chain", "solana")
    n = src.get("n", config.DEFAULTS["max_candidates"])
    locked: list[str] = []
    kind = src.get("type", "trending")

    if kind == "megafilter" and not caps.get("analyst"):
        locked.append("🔒 megafilter needs the Analyst plan: https://www.coingecko.com/en/api/pricing — falling back to trending pools")
        kind = "trending"

    if kind == "new_pools":
        rows = await client.new_pools(chain, n)
        note = f"new_pools/{chain}"
    elif kind == "megafilter":
        rows = await client.megafilter(**(src.get("megafilter_params") or {"checks": "no_honeypot,good_gt_score"}))
        note = "megafilter"
    else:
        duration = src.get("duration", "1h")
        rows = await client.trending_pools(chain, duration, n)
        note = f"trending/{chain}/{duration}"

    return [normalize_pool(r, chain) for r in rows], note, locked


async def enrich_and_screen(client: CoinGeckoClient, strategy: Strategy, candidates: list[dict], caps: dict) -> tuple[list[Evaluation], list[str]]:
    """Runs the cheap pool-tier checks on every candidate, then spends credits on token_info / top_traders
    only for survivors, capped by config so a scan can never runaway-spend."""
    locked: list[str] = []
    evaluations = [strategy.evaluate_pool_tier(c) for c in candidates]
    survivors = [e for e in evaluations if e.passed]

    if strategy.needs_token_info:
        for ev in survivors[: config.DEFAULTS["max_token_info_calls"]]:
            info = None
            try:
                raw = await client.token_info(ev.candidate["chain"], ev.candidate["address"])
                info = normalize_token_info(raw)
            except PlanRestrictedError:
                locked.append("🔒 token_info (GT Score/honeypot/holders) needs a paid plan: https://www.coingecko.com/en/api/pricing")
            except Exception:
                info = None
            strategy.apply_token_info(ev, info)
        for ev in survivors[config.DEFAULTS["max_token_info_calls"] :]:
            ev.checks.append(CheckResult("token_info", False, "skipped: this scan's token_info budget was already spent on higher-ranked candidates"))
        survivors = [e for e in evaluations if e.passed]

    if strategy.needs_smart_money:
        if not caps.get("analyst"):
            locked.append("🔒 smart-money gate needs the Analyst plan: https://www.coingecko.com/en/api/pricing")
            for ev in survivors:
                strategy.apply_smart_money(ev, None)
        else:
            for ev in survivors[: config.DEFAULTS["max_smart_money_calls"]]:
                traders = []
                try:
                    token_id = ev.candidate.get("id") or ""
                    token_address = token_id.split("_", 1)[1] if "_" in token_id else ev.candidate["address"]
                    traders = await client.top_traders(ev.candidate["chain"], token_address, n=10)
                except PlanRestrictedError:
                    locked.append("🔒 top_traders needs the Analyst plan: https://www.coingecko.com/en/api/pricing")
                except Exception:
                    traders = []
                strategy.apply_smart_money(ev, traders)

    return evaluations, locked


async def run_scan(client: CoinGeckoClient, strategy: Strategy, caps: dict) -> ScanResult:
    """One full scan: fetch -> screen -> return a ScanResult with every candidate's full check trail."""
    before_credits = client.credits_used
    candidates, note, locked_source = await fetch_candidates(client, strategy, caps)
    evaluations, locked_screen = await enrich_and_screen(client, strategy, candidates, caps)
    return ScanResult(
        ts=time.time(),
        strategy_name=strategy.name,
        source_note=note,
        candidates=candidates,
        evaluations=evaluations,
        locked=locked_source + locked_screen,
        credits_used=client.credits_used - before_credits,
    )


def record_scan(scan: ScanResult, scans_dir: str | Path = config.SCANS_DIR):
    """Appends this scan's full record (inputs + every check) to data/scans/<strategy>.jsonl, for `backtest --replay-scans`."""
    path = Path(scans_dir)
    path.mkdir(parents=True, exist_ok=True)
    with (path / f"{scan.strategy_name}.jsonl").open("a") as fh:
        fh.write(json.dumps(scan.as_dict()) + "\n")


def read_scans(strategy_name: str, scans_dir: str | Path = config.SCANS_DIR) -> list[dict]:
    """Every recorded scan for this strategy, oldest first."""
    path = Path(scans_dir) / f"{strategy_name}.jsonl"
    if not path.exists():
        return []
    with path.open() as fh:
        return [json.loads(line) for line in fh if line.strip()]
