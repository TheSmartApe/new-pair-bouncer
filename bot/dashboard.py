"""The rich live terminal dashboard: scan stats, filter reasons, open positions, P&L, credits, next-scan countdown."""
import time
from dataclasses import dataclass, field

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


@dataclass
class DashboardState:
    """Everything the dashboard needs to redraw itself; cli.py mutates this each scan/tick."""

    strategy_name: str = ""
    environment: str = "pro"
    plan_note: str = ""
    mode: str = "forward"
    scan_count: int = 0
    last_scan_note: str = ""
    last_candidates: int = 0
    last_passed: int = 0
    last_checks: list[dict] = field(default_factory=list)  # [{symbol, passed, checks:[...]}]
    locked: list[str] = field(default_factory=list)
    open_positions: list[dict] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    credits_used: int = 0
    max_credits_per_day: float | None = None
    next_scan_ts: float = 0
    started_ts: float = field(default_factory=time.time)
    recent_decisions: list[dict] = field(default_factory=list)  # entries/exits, most recent last


def _reasons_table(state: DashboardState) -> Table:
    table = Table(title=f"Last scan: {state.last_scan_note} ({state.last_passed}/{state.last_candidates} passed)", expand=True)
    table.add_column("Candidate")
    table.add_column("Result")
    table.add_column("Reasons")
    for row in state.last_checks[:12]:
        result = Text("PASS", style="bold green") if row["passed"] else Text("FAIL", style="bold red")
        reasons = "; ".join(f"{c['name']}: {c['reason']}" for c in row["checks"] if not c["passed"]) or "all checks passed"
        table.add_row(row["symbol"] or "?", result, reasons)
    if not state.last_checks:
        table.add_row("(no scan yet)", "", "")
    return table


def _positions_table(state: DashboardState) -> Table:
    table = Table(title="Open paper positions", expand=True)
    table.add_column("Symbol")
    table.add_column("Entry")
    table.add_column("Price")
    table.add_column("P&L")
    table.add_column("Hold")
    for row in state.open_positions:
        pnl_style = "green" if row["change_pct"] >= 0 else "red"
        table.add_row(
            row["symbol"],
            f"${row['entry_price']:.4g}",
            f"${row['price']:.4g}",
            Text(f"{row['change_pct']:+.1f}%", style=pnl_style),
            f"{row['hold_minutes']:.0f}m",
        )
    if not state.open_positions:
        table.add_row("(none)", "", "", "", "")
    return table


def _header(state: DashboardState) -> Panel:
    countdown = max(0, round(state.next_scan_ts - time.time()))
    uptime_min = round((time.time() - state.started_ts) / 60)
    budget = f"{state.credits_used}/{int(state.max_credits_per_day)}" if state.max_credits_per_day else str(state.credits_used)
    lines = [
        f"[bold]{state.strategy_name}[/bold]  ·  {state.mode}  ·  {state.environment} key{'  ·  ' + state.plan_note if state.plan_note else ''}",
        f"scans: {state.scan_count}   uptime: {uptime_min}min   credits used: {budget}   next scan in: {countdown}s",
    ]
    if state.locked:
        lines.append("")
        lines.extend(state.locked)
    return Panel(Text.from_markup("\n".join(lines)), title="onchain-signal-bot", border_style="green")


def _metrics_panel(state: DashboardState) -> Panel:
    m = state.metrics or {}
    text = (
        f"trades: {m.get('trades', 0)}   win rate: {m.get('win_rate')}   "
        f"P&L: ${m.get('pnl_usd', 0):.2f} ({m.get('pnl_pct', 0)}%)   max drawdown: {m.get('max_drawdown_pct', 0)}%"
    )
    return Panel(text, title="Paper P&L", border_style="cyan")


def _recent_panel(state: DashboardState) -> Panel:
    lines = []
    for d in state.recent_decisions[-6:]:
        if d.get("action") == "entry":
            lines.append(f"[green]ENTRY[/green]  {d.get('symbol')}  @ ${d.get('price_usd'):.4g}  (${d.get('budget_usd')})")
        elif d.get("action") == "exit":
            lines.append(f"[yellow]EXIT[/yellow]   {d.get('symbol')}  {d.get('reason')}")
        elif d.get("action") == "entry_skipped":
            lines.append(f"[dim]skip[/dim]    {d.get('symbol')}  {d.get('reason')}")
    if not lines:
        lines = ["(no entries or exits yet)"]
    return Panel(Text.from_markup("\n".join(lines)), title="Recent decisions", border_style="magenta")


def render(state: DashboardState) -> Group:
    return Group(_header(state), _reasons_table(state), _positions_table(state), _metrics_panel(state), _recent_panel(state))


def make_console() -> Console:
    return Console()
