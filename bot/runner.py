"""The live loop shared by `forward` and `autopilot`: scan -> screen -> paper trade -> monitor exits -> dashboard.

Restart-safe: portfolio + open-position metadata are checkpointed to state.db after every cycle, so killing and
restarting an autopilot run resumes the same positions and P&L rather than starting over.
"""
import asyncio
import logging
from datetime import date
from pathlib import Path

from rich.live import Live

from core import assumptions as assumptions_mod
from core.autopilot import Autopilot, Job
from core.client import CoinGeckoClient
from core.paper import ClosedTrade, Portfolio, Position
from core.plan import probe_capabilities
from core.store import Store

from . import config, dashboard, recap, runstore
from .engine import Engine, PositionMeta
from .scan import record_scan, run_scan
from .wsfeed import WsFeed

log = logging.getLogger("bot.runner")


def _portfolio_to_state(p: Portfolio) -> dict:
    return {
        "cash": p.cash,
        "starting_cash": p.starting_cash,
        "positions": {k: {"qty": v.qty, "cost_usd": v.cost_usd, "opened_ts": v.opened_ts} for k, v in p.positions.items()},
        "closed": [
            {"symbol": t.symbol, "qty": t.qty, "buy_usd": t.buy_usd, "sell_usd": t.sell_usd, "pnl_usd": t.pnl_usd, "opened_ts": t.opened_ts, "closed_ts": t.closed_ts}
            for t in p.closed
        ],
        "equity_curve": p.equity_curve,
    }


def _portfolio_from_state(state: dict, assumptions: dict) -> Portfolio:
    p = Portfolio(
        cash=state.get("cash", assumptions["starting_cash"]),
        slippage_bps=assumptions["slippage_bps"],
        fee_bps=assumptions["fee_bps"],
        max_position_pct=assumptions["max_position_pct"],
        cooldown_s=assumptions["cooldown_s"],
    )
    p.starting_cash = state.get("starting_cash", assumptions["starting_cash"])
    p.positions = {k: Position(**v) for k, v in state.get("positions", {}).items()}
    p.closed = [ClosedTrade(**t) for t in state.get("closed", [])]
    p.equity_curve = [tuple(pt) for pt in state.get("equity_curve", [])]
    return p


async def run(
    strategy,
    mode: str,
    run_dir: Path,
    minutes: float | None = None,
    interval_s: float | None = None,
    max_credits_per_day: float | None = None,
    starting_cash: float | None = None,
    use_websocket: bool = True,
    max_seconds: float | None = None,
) -> dict:
    """Runs the scan/monitor loop. Returns final {metrics, credits_used, run_dir}."""
    assumptions = assumptions_mod.load("assumptions.yaml")
    if starting_cash:
        assumptions["starting_cash"] = starting_cash
    interval_s = interval_s or config.DEFAULTS["interval_s"]

    client = CoinGeckoClient()
    caps = await probe_capabilities(client)

    store = Store(run_dir / "state.db")
    state = store.get("portfolio")
    portfolio = _portfolio_from_state(state, assumptions) if state else Portfolio(
        cash=assumptions["starting_cash"],
        slippage_bps=assumptions["slippage_bps"],
        fee_bps=assumptions["fee_bps"],
        max_position_pct=assumptions["max_position_pct"],
        cooldown_s=assumptions["cooldown_s"],
    )
    engine = Engine(portfolio=portfolio, strategy=strategy)
    for key, m in (store.get("meta") or {}).items():
        engine.meta[key] = PositionMeta(**m)

    ws_feed: WsFeed | None = None
    if caps.get("websocket") and use_websocket:
        ws_feed = WsFeed(engine.open_pool_keys)
        ws_feed.start()
        engine.ws_prices = ws_feed.prices

    plan_note = "Analyst plan detected" if caps.get("analyst") else ("paid plan, REST-only Beta features" if caps.get("paid") else "Demo key: REST-only mode")
    state_view = dashboard.DashboardState(
        strategy_name=strategy.name,
        environment="demo" if not caps.get("paid") else "pro",
        plan_note=plan_note,
        mode=mode,
        max_credits_per_day=max_credits_per_day,
    )

    def _credits_today() -> float:
        today = date.today().isoformat()
        if store.get("credit_day") != today:
            store.set("credit_day", today)
            store.set("credits_at_day_start", client.credits_used)
        base = store.get("credits_at_day_start", 0)
        return client.credits_used - base

    async def _persist():
        store.set("portfolio", _portfolio_to_state(engine.portfolio))
        store.set("meta", {k: {"chain": m.chain, "address": m.address, "symbol": m.symbol, "strategy": m.strategy} for k, m in engine.meta.items()})

    async def scan_job():
        scan = await run_scan(client, strategy, caps)
        record_scan(scan)
        runstore.append_decision(run_dir, {"kind": "scan", **scan.as_dict()})
        state_view.scan_count += 1
        state_view.last_scan_note = scan.source_note
        state_view.last_candidates = len(scan.candidates)
        state_view.last_passed = len(scan.passed)
        state_view.last_checks = [e.as_dict() for e in scan.evaluations]
        for note in scan.locked:
            if note not in state_view.locked:
                state_view.locked.append(note)
        for ev in scan.passed:
            decision = engine.try_enter(ev)
            if decision:
                runstore.append_decision(run_dir, {"kind": "decision", **decision})
                if decision.get("action") == "entry":
                    state_view.recent_decisions.append(decision)
        if ws_feed:
            await ws_feed.resync()
        await _persist()

    last_prices: dict[str, float] = {}

    async def monitor_job():
        decisions, prices = await _monitor(engine, client)
        last_prices.clear()
        last_prices.update(prices)
        for d in decisions:
            runstore.append_decision(run_dir, {"kind": "decision", **d})
            state_view.recent_decisions.append(d)
        state_view.open_positions = engine.open_positions_view(prices)
        state_view.metrics = engine.portfolio.metrics(prices)
        state_view.credits_used = client.credits_used
        await _persist()

    autopilot = Autopilot(
        jobs=[Job("scan", interval_s, scan_job), Job("monitor", config.DEFAULTS["price_poll_s"], monitor_job)],
        store=store,
        credits_today_fn=_credits_today,
        max_credits_per_day=max_credits_per_day,
        heartbeat_s=config.DEFAULTS["heartbeat_s"],
    )

    console = dashboard.make_console()
    stop_render = asyncio.Event()

    async def render_loop():
        with Live(dashboard.render(state_view), console=console, refresh_per_second=2, screen=False) as live:
            while not stop_render.is_set():
                last_scan = store.get("last_run:scan", 0)
                state_view.next_scan_ts = last_scan + interval_s
                live.update(dashboard.render(state_view))
                await asyncio.sleep(0.5)

    autopilot_task = asyncio.create_task(autopilot.run_forever())
    render_task = asyncio.create_task(render_loop())
    try:
        if minutes is not None:
            await asyncio.sleep(minutes * 60)
            autopilot.stop()
            await autopilot_task
        elif max_seconds is None:
            await autopilot_task
        else:
            try:
                await asyncio.wait_for(autopilot_task, timeout=max_seconds)
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        autopilot.stop()
    finally:
        stop_render.set()
        render_task.cancel()
        try:
            await render_task
        except asyncio.CancelledError:
            pass
        if ws_feed:
            await ws_feed.stop()
        metrics = engine.portfolio.metrics(last_prices)
        runstore.write_metrics(
            run_dir,
            {
                **metrics,
                "credits_used": client.credits_used,
                "scans": state_view.scan_count,
                "environment": state_view.environment,
                "plan_note": state_view.plan_note,
                "strategy": strategy.name,
                "mode": mode,
            },
        )
        runstore.write_equity_curve(run_dir, engine.portfolio.equity_curve)
        runstore.write_trades_csv(run_dir, engine.portfolio.closed)
        decisions = runstore.read_decisions(run_dir)
        recap.build(run_dir, strategy.name, decisions, metrics, client.credits_used)
        await _persist()
        store.close()
        await client.close()

    return {"metrics": metrics, "credits_used": client.credits_used, "run_dir": str(run_dir)}


async def _monitor(engine: Engine, client: CoinGeckoClient):
    """monitor_open_positions, but also returns the prices it fetched so the dashboard/metrics can reuse them."""
    decisions = await engine.monitor_open_positions(client)
    prices = {}
    for key, meta in list(engine.meta.items()):
        price = await engine.current_price(client, meta.chain, meta.address)
        if price:
            prices[key] = price
    return decisions, prices
