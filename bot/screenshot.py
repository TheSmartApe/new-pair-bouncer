"""Renders the rich terminal dashboard to a PNG for docs/screens/ and the article kit.

rich.Console(record=True) -> save_svg(), then Playwright screenshots the SVG. Needs Playwright installed
(`uv pip install playwright && playwright install chromium`), same as core.articlekit.
"""
import time
from pathlib import Path

from rich.console import Console

from . import dashboard, runstore


def state_from_run(run_id: str) -> dashboard.DashboardState:
    """Rebuilds a representative DashboardState from a completed run's own decisions + metrics — no fabricated data."""
    run_dir = runstore.resolve_run(run_id)
    decisions = runstore.read_decisions(run_dir)
    metrics = runstore.read_metrics(run_dir)
    scans = [d for d in decisions if d.get("kind") == "scan"]
    entries = {d["address"]: d for d in decisions if d.get("kind") == "decision" and d.get("action") == "entry"}
    exited = {d["address"] for d in decisions if d.get("kind") == "decision" and d.get("action") == "exit"}
    open_addrs = [a for a in entries if a not in exited]

    equity_curve = runstore.read_equity_curve(run_dir)
    state = dashboard.DashboardState(
        strategy_name=metrics.get("strategy", run_dir.name),
        environment=metrics.get("environment", "pro"),
        plan_note=metrics.get("plan_note", ""),
        mode=metrics.get("mode", "forward"),
        scan_count=len(scans),
        credits_used=metrics.get("credits_used", 0),
        max_credits_per_day=metrics.get("max_credits_per_day"),
        metrics=metrics,
        equity_curve=equity_curve,
        interval_s=metrics.get("interval_s", 60),
        next_scan_ts=time.time() + 42,
    )
    if scans:
        last = scans[-1]
        state.last_scan_note = last.get("source_note", "")
        state.last_candidates = last.get("candidate_count", 0)
        state.last_checks = last.get("evaluations", [])
        state.last_passed = sum(1 for e in state.last_checks if e.get("passed"))
        for s in scans:
            for note in s.get("locked", []):
                if note not in state.locked:
                    state.locked.append(note)
    state.recent_decisions = [d for d in decisions if d.get("kind") == "decision"][-8:]
    state.open_positions = [
        {
            "symbol": entries[a].get("symbol"),
            "chain": entries[a].get("chain"),
            "pair": entries[a].get("symbol"),
            "entry_price": entries[a].get("price_usd", 0),
            "price": entries[a].get("price_usd", 0),
            "change_pct": 0.0,
            "hold_minutes": round((time.time() - entries[a].get("ts", time.time())) / 60, 1),
        }
        for a in open_addrs
    ]
    return state


def render_svg(state: dashboard.DashboardState, out_svg: Path, width: int = 1280):
    console = Console(record=True, width=width)
    console.print(dashboard.render(state))
    console.save_svg(str(out_svg), title="onchain-signal-bot")


def svg_to_png(svg_path: Path, png_path: Path, width: int = 1280, height: int = 900):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright isn't installed. Run: uv pip install playwright && playwright install chromium") from exc
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto(svg_path.resolve().as_uri())
        page.screenshot(path=str(png_path))
        browser.close()


def screenshot_run(run_id: str, out_path: str | Path):
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    state = state_from_run(run_id)
    svg_path = out_path.with_suffix(".svg")
    render_svg(state, svg_path)
    svg_to_png(svg_path, out_path)
    return out_path
