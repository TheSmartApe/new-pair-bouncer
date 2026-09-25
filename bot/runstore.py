"""Run-directory conventions: runs/<id>/{decisions.jsonl, trades.csv, metrics.json, recap.md, state.db}."""
import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .config import RUNS_DIR


def new_run_dir(mode: str, strategy_name: str) -> Path:
    """A fresh timestamped dir for a backtest or a bounded forward test."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = Path(RUNS_DIR) / f"{stamp}-{mode}-{strategy_name}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def autopilot_run_dir(strategy_name: str) -> Path:
    """A stable dir reused across restarts, so autopilot resumes the same run instead of starting a new one."""
    # The evaluator can opt into an isolated namespace while normal users retain restart-safe
    # behavior across invocations.
    namespace = os.environ.get("COINGECKO_RUN_NAMESPACE")
    prefix = f"{namespace}-" if namespace else ""
    path = Path(RUNS_DIR) / f"{prefix}autopilot-{strategy_name}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_run(run_id: str) -> Path:
    """`latest` -> the most recently modified runs/ dir; otherwise runs/<run_id> (or the id used as-is if it's already a path)."""
    if run_id in (None, "latest"):
        candidates = [p for p in Path(RUNS_DIR).iterdir() if p.is_dir()] if Path(RUNS_DIR).exists() else []
        if not candidates:
            raise FileNotFoundError("no runs yet — run `make forward` or `make backtest` first")
        return max(candidates, key=lambda p: p.stat().st_mtime)
    direct = Path(run_id)
    if direct.exists():
        return direct
    return Path(RUNS_DIR) / run_id


def append_decision(run_dir: Path, data: dict):
    """Appends one JSON line to decisions.jsonl, timestamped."""
    line = {"ts": datetime.now(timezone.utc).timestamp(), **data}
    with (run_dir / "decisions.jsonl").open("a") as fh:
        fh.write(json.dumps(line) + "\n")


def read_decisions(run_dir: Path) -> list[dict]:
    path = run_dir / "decisions.jsonl"
    if not path.exists():
        return []
    with path.open() as fh:
        return [json.loads(line) for line in fh if line.strip()]


def write_trades_csv(run_dir: Path, closed_trades: list):
    """Writes trades.csv from a list of core.paper.ClosedTrade."""
    path = run_dir / "trades.csv"
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["symbol", "qty", "buy_usd", "sell_usd", "pnl_usd", "opened_ts", "closed_ts"])
        for t in closed_trades:
            writer.writerow([t.symbol, t.qty, round(t.buy_usd, 2), round(t.sell_usd, 2), round(t.pnl_usd, 2), t.opened_ts, t.closed_ts])


def write_metrics(run_dir: Path, metrics: dict):
    (run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2))


def read_metrics(run_dir: Path) -> dict:
    path = run_dir / "metrics.json"
    return json.loads(path.read_text()) if path.exists() else {}


def write_equity_curve(run_dir: Path, curve: list):
    (run_dir / "equity_curve.json").write_text(json.dumps(curve))


def read_equity_curve(run_dir: Path) -> list[tuple[float, float]]:
    path = run_dir / "equity_curve.json"
    if not path.exists():
        return []
    return [tuple(pt) for pt in json.loads(path.read_text())]
