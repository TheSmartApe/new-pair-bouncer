"""Two honest backtests (see README): replay recorded scans against a strategy's current thresholds (no network,
no lookahead — every input was already observed at scan time), and test exit rules against the historical OHLCV
of pools a previous run actually flagged with a paper entry.

New-pool listings can't be pulled for past dates, so this is a replay of real scans, not a full-history backtest.
"""
from core.client import CoinGeckoClient
from core.paper import Portfolio

from . import runstore
from .scan import read_scans
from .strategy import Strategy


def replay_scans(strategy: Strategy, scans_dir: str = "data/scans") -> dict:
    """Re-applies `strategy`'s CURRENT filter thresholds to every candidate from previously recorded scans."""
    records = read_scans(strategy.name, scans_dir)
    total = 0
    then_passed = 0
    now_passed = 0
    agreed = 0
    changed: list[dict] = []
    for rec in records:
        for ev_dict in rec.get("evaluations", []):
            candidate = ev_dict.get("candidate")
            if not candidate:
                continue  # a scan recorded before this repo tracked full candidate inputs
            total += 1
            ev = strategy.evaluate_from_record(candidate, ev_dict.get("token_info"), ev_dict.get("best_trader_pnl_usd"))
            if ev.passed:
                now_passed += 1
            if ev_dict.get("passed"):
                then_passed += 1
            if ev.passed == ev_dict.get("passed"):
                agreed += 1
            elif len(changed) < 20:
                changed.append({"symbol": candidate.get("symbol"), "passed_at_scan_time": ev_dict.get("passed"), "passes_now": ev.passed})
    return {
        "strategy": strategy.name,
        "scans_replayed": len(records),
        "candidates_replayed": total,
        "passed_at_scan_time": then_passed,
        "passes_under_current_thresholds": now_passed,
        "agreement": round(agreed / total, 3) if total else None,
        "changed_examples": changed,
    }


async def backtest_exits(strategy: Strategy, client: CoinGeckoClient, entries: list[dict], pages: int = 2) -> dict:
    """Replays each flagged pool's OHLCV forward from its entry point through THIS strategy's exit rules."""
    portfolio = Portfolio(cash=max(1000.0, sum(e.get("budget_usd", 50) for e in entries) or 1000.0), slippage_bps=30, fee_bps=25, max_position_pct=1.0, cooldown_s=0)
    results = []
    for e in entries:
        chain, address, entry_price, entry_ts = e.get("chain"), e.get("address"), e.get("price_usd"), e.get("ts")
        symbol = e.get("symbol") or address
        if not (chain and address and entry_price and entry_ts):
            continue
        try:
            candles = await client.pool_ohlcv_history(chain, address, "minute", aggregate=1, pages=pages)
        except Exception as exc:
            results.append({"symbol": symbol, "reason": "no_data", "error": type(exc).__name__})
            continue
        after = sorted((c for c in candles if c and c[0] >= entry_ts), key=lambda c: c[0])
        if not after:
            results.append({"symbol": symbol, "reason": "no_candles_after_entry", "change_pct": None})
            continue
        portfolio.buy(symbol, entry_ts, entry_price, usd=e.get("budget_usd", 50))
        portfolio.snapshot(entry_ts, {symbol: entry_price})
        exit_reason, exit_ts, exit_price, change_pct = None, None, None, None
        for candle in after:
            ts, close = candle[0], candle[4]
            change_pct = (close - entry_price) / entry_price * 100
            hold_min = (ts - entry_ts) / 60
            if strategy.exits.get("take_profit_pct") is not None and change_pct >= strategy.exits["take_profit_pct"]:
                exit_reason = "take_profit"
            elif strategy.exits.get("stop_loss_pct") is not None and change_pct <= -strategy.exits["stop_loss_pct"]:
                exit_reason = "stop_loss"
            elif strategy.exits.get("max_hold_minutes") is not None and hold_min >= strategy.exits["max_hold_minutes"]:
                exit_reason = "max_hold"
            if exit_reason:
                exit_ts, exit_price = ts, close
                break
        if not exit_reason:
            exit_reason = "end_of_available_data"
            exit_ts, exit_price = after[-1][0], after[-1][4]
            change_pct = (exit_price - entry_price) / entry_price * 100
        portfolio.sell(symbol, exit_ts, exit_price)
        portfolio.snapshot(exit_ts, {symbol: exit_price})
        results.append({"symbol": symbol, "reason": exit_reason, "change_pct": round(change_pct, 2) if change_pct is not None else None})
    return {"strategy": strategy.name, "positions_tested": len(results), "exits": results, "metrics": portfolio.metrics()}


def entries_from_run(run_id: str) -> list[dict]:
    """Every paper entry a previous run logged, read straight from its decisions.jsonl."""
    run_dir = runstore.resolve_run(run_id)
    decisions = runstore.read_decisions(run_dir)
    return [d for d in decisions if d.get("kind") == "decision" and d.get("action") == "entry"]
