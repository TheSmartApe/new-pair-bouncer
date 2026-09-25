"""Builds the daily 'while you slept' recap: pools screened, alerts, paper trades, P&L, credits."""
from datetime import datetime, timezone
from pathlib import Path


def build(run_dir: Path, strategy_name: str, decisions: list[dict], metrics: dict, credits_used: int, window_hours: float | None = None) -> str:
    """Renders recap.md from the run's own decisions.jsonl + metrics.json. Returns the markdown text."""
    scans = [d for d in decisions if d.get("kind") == "scan"]
    entries = [d for d in decisions if d.get("kind") == "decision" and d.get("action") == "entry"]
    exits = [d for d in decisions if d.get("kind") == "decision" and d.get("action") == "exit"]
    candidates_seen = sum(d.get("candidate_count", 0) for d in scans)
    passed_seen = sum(len(d.get("evaluations", [])) and sum(1 for e in d.get("evaluations", []) if e.get("passed")) for d in scans)
    now = datetime.now(timezone.utc)
    window_note = f"the last {window_hours:.1f}h" if window_hours else "this run"

    lines = [
        f"# {strategy_name} — while you slept",
        "",
        f"_Generated {now.strftime('%Y-%m-%d %H:%M UTC')}, covering {window_note}._",
        "",
        "## Summary",
        "",
        f"- Scans run: {len(scans)}",
        f"- Pools screened: {candidates_seen}",
        f"- Candidates that passed every filter: {passed_seen}",
        f"- Paper entries: {len(entries)}",
        f"- Paper exits: {len(exits)}",
        f"- Credits used: {credits_used}",
        "",
        "## Paper P&L",
        "",
        f"- Trades closed: {metrics.get('trades', 0)}",
        f"- Win rate: {metrics.get('win_rate')}",
        f"- P&L: ${metrics.get('pnl_usd', 0):.2f} ({metrics.get('pnl_pct', 0)}%)",
        f"- Max drawdown: {metrics.get('max_drawdown_pct', 0)}%",
        "",
        "## Entries",
        "",
    ]
    if entries:
        for e in entries[-20:]:
            lines.append(f"- **{e.get('symbol')}** @ ${e.get('price_usd', 0):.4g} (${e.get('budget_usd')} budget)")
    else:
        lines.append("- (none this run)")
    lines += ["", "## Exits", ""]
    if exits:
        for e in exits[-20:]:
            lines.append(f"- **{e.get('symbol')}**: {e.get('reason')} ({e.get('change_pct'):+.1f}%)")
    else:
        lines.append("- (none this run)")
    lines += [
        "",
        "---",
        "_Paper trading only. CoinGecko API provides market data; it doesn't execute trades or give financial advice._",
        "",
    ]
    text = "\n".join(lines)
    (run_dir / "recap.md").write_text(text)
    return text
