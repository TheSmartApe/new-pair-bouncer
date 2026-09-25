"""The rich live terminal dashboard: branded header, chain/pool-aware filter pass/fail, equity
sparkline, live positions, credits meter, next-scan countdown, and a recent-decisions feed.

Every table pads to a fixed row count and fixes its column widths (no_wrap + ellipsis overflow),
so the dashboard's total line count never changes between frames -- the thing that actually
causes a `rich.Live` terminal to flicker or jump around during a recording. Designed to read
clearly at a typical 120x35 recording terminal.
"""
import time
from dataclasses import dataclass, field

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from core import config as core_config

# CoinGecko API "Moon" primary green.
GREEN = "#8dc63f"
RED = "#e2574c"
DIM = "grey62"

REASON_ROWS = 6
POSITION_ROWS = 5
DECISION_LINES = 5
SPARK_WIDTH = 28
BLOCKS = "▁▂▃▄▅▆▇█"


@dataclass
class DashboardState:
    """Everything the dashboard needs to redraw itself; cli.py/runner.py mutate this each scan/tick."""

    strategy_name: str = ""
    environment: str = "pro"
    plan_note: str = ""
    mode: str = "forward"
    scan_count: int = 0
    last_scan_note: str = ""
    last_candidates: int = 0
    last_passed: int = 0
    last_checks: list[dict] = field(default_factory=list)  # [{symbol, chain, passed, checks:[...], candidate:{...}}]
    locked: list[str] = field(default_factory=list)
    open_positions: list[dict] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    equity_curve: list = field(default_factory=list)  # [(ts, equity_usd), ...]
    credits_used: int = 0
    max_credits_per_day: float | None = None
    next_scan_ts: float = 0
    interval_s: float = 60
    started_ts: float = field(default_factory=time.time)
    recent_decisions: list[dict] = field(default_factory=list)  # entries/exits, most recent last
    network_names: dict = field(default_factory=dict)  # {network_id: display name}, from /onchain/networks


def _chain_label(state: DashboardState, chain_id: str | None) -> str:
    if not chain_id:
        return "-"
    return state.network_names.get(chain_id) or core_config.CHAINS.get(chain_id, chain_id)


def _bar(fraction: float, width: int = 14, style: str = GREEN) -> Text:
    fraction = max(0.0, min(1.0, fraction))
    filled = round(fraction * width)
    t = Text()
    t.append("█" * filled, style=style)
    t.append("░" * (width - filled), style=DIM)
    return t


def _sparkline(values: list[float], width: int = SPARK_WIDTH) -> Text:
    """Unicode-block sparkline of the equity curve's most recent `width` points, colored by net direction."""
    if len(values) < 2:
        return Text("·" * width, style=DIM)
    vals = values[-width:]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    chars = "".join(BLOCKS[int((v - lo) / span * (len(BLOCKS) - 1))] for v in vals)
    chars = chars.rjust(width, "·")
    up = vals[-1] >= vals[0]
    return Text(chars, style=GREEN if up else RED)


def _wordmark() -> Text:
    t = Text()
    t.append("◆ ", style=f"bold {GREEN}")
    t.append("C", style=f"bold {GREEN}")
    t.append("OIN", style=f"bold {GREEN}")
    t.append("G", style=f"bold {GREEN}")
    t.append("ECKO ", style=f"bold {GREEN}")
    t.append("A", style=f"bold {GREEN}")
    t.append("PI", style=f"bold {GREEN}")
    t.append("   onchain-signal-bot", style="dim")
    return t


def _header(state: DashboardState) -> Panel:
    now = time.time()
    remaining = max(0, state.next_scan_ts - now)
    countdown_frac = 1 - (remaining / state.interval_s) if state.interval_s else 1
    uptime_min = round((now - state.started_ts) / 60)
    cap = state.max_credits_per_day
    credits_frac = (state.credits_used / cap) if cap else 0
    credits_style = GREEN if credits_frac < 0.8 else ("yellow" if credits_frac < 0.95 else RED)
    budget_label = f"{state.credits_used}/{int(cap)}" if cap else f"{state.credits_used}"

    equity = state.equity_curve[-1][1] if state.equity_curve else None
    pnl_usd = state.metrics.get("pnl_usd", 0) or 0
    pnl_pct = state.metrics.get("pnl_pct", 0) or 0
    pnl_style = GREEN if pnl_usd >= 0 else RED

    grid = Table.grid(expand=True)
    grid.add_column()
    grid.add_row(_wordmark())
    grid.add_row(Text("Data: CoinGecko API · coingecko.com/en/api", style="dim"))
    grid.add_row(Text(""))

    meta_line = Text()
    meta_line.append(f"{state.strategy_name}", style="bold")
    meta_line.append(f"  ·  {state.mode}  ·  {state.environment} key", style=DIM)
    if state.plan_note:
        meta_line.append(f"  ·  {state.plan_note}", style="cyan")
    grid.add_row(meta_line)

    stats_line = Text()
    stats_line.append("scans ", style=DIM)
    stats_line.append(f"{state.scan_count}", style="bold")
    stats_line.append("   uptime ", style=DIM)
    stats_line.append(f"{uptime_min}m", style="bold")
    stats_line.append("   credits ", style=DIM)
    stats_line.append_text(_bar(credits_frac, 14, credits_style))
    stats_line.append(f" {budget_label}", style=credits_style)
    stats_line.append("   next scan ", style=DIM)
    stats_line.append_text(_bar(countdown_frac, 10, "cyan"))
    stats_line.append(f" {remaining:.0f}s", style="cyan")
    grid.add_row(stats_line)

    equity_line = Text()
    equity_line.append("equity ", style=DIM)
    equity_line.append_text(_sparkline([v for _, v in state.equity_curve]))
    if equity is not None:
        equity_line.append(f"  ${equity:,.2f}", style="bold")
        equity_line.append(f" ({pnl_usd:+,.2f}, {pnl_pct:+.2f}%)", style=pnl_style)
    grid.add_row(equity_line)

    if state.locked:
        grid.add_row(Text(""))
        for note in state.locked[:2]:
            grid.add_row(Text(note, style="yellow"))

    return Panel(grid, border_style=GREEN, title="onchain-signal-bot", title_align="left")


def _result_cell(passed: bool) -> Text:
    return Text("✓ PASS", style=f"bold {GREEN}") if passed else Text("✗ FAIL", style=f"bold {RED}")


def _reasons_table(state: DashboardState) -> Panel:
    table = Table(expand=True, show_edge=False, pad_edge=False)
    table.add_column("Chain", width=10, no_wrap=True)
    table.add_column("Pool", width=20, no_wrap=True, overflow="ellipsis")
    table.add_column("Result", width=9, no_wrap=True)
    table.add_column("Reasons", no_wrap=True, overflow="ellipsis")

    rows = state.last_checks[:REASON_ROWS]
    for row in rows:
        candidate = row.get("candidate") or {}
        pair = candidate.get("name") or row.get("symbol") or "?"
        chain = _chain_label(state, row.get("chain") or candidate.get("chain"))
        reasons = "; ".join(f"{c['name']}: {c['reason']}" for c in row["checks"] if not c["passed"]) or "all checks passed"
        table.add_row(chain, pair, _result_cell(row["passed"]), reasons)
    for _ in range(REASON_ROWS - len(rows)):
        table.add_row("", "", "", "")
    title = f"Last scan: {state.last_scan_note or '(none yet)'}  —  {state.last_passed}/{state.last_candidates} passed"
    return Panel(table, title=title, title_align="left", border_style="white")


def _pnl_cell(change_pct: float) -> Text:
    style = GREEN if change_pct >= 0 else RED
    arrow = "▲" if change_pct >= 0 else "▼"
    return Text(f"{arrow} {change_pct:+.1f}%", style=style)


def _positions_table(state: DashboardState) -> Panel:
    table = Table(expand=True, show_edge=False, pad_edge=False)
    table.add_column("Chain", width=10, no_wrap=True)
    table.add_column("Pool", width=18, no_wrap=True, overflow="ellipsis")
    table.add_column("Entry", width=12, no_wrap=True, justify="right")
    table.add_column("Price", width=12, no_wrap=True, justify="right")
    table.add_column("P&L", width=10, no_wrap=True, justify="right")
    table.add_column("Hold", width=7, no_wrap=True, justify="right")

    rows = state.open_positions[:POSITION_ROWS]
    for row in rows:
        table.add_row(
            _chain_label(state, row.get("chain")),
            row.get("pair") or row.get("symbol", "?"),
            f"${row['entry_price']:.4g}",
            f"${row['price']:.4g}",
            _pnl_cell(row["change_pct"]),
            f"{row['hold_minutes']:.0f}m",
        )
    for _ in range(POSITION_ROWS - len(rows)):
        table.add_row("", "", "", "", "", "", style=DIM)
    return Panel(table, title=f"Open paper positions ({len(state.open_positions)})", title_align="left", border_style="cyan")


def _recent_panel(state: DashboardState) -> Panel:
    lines: list[Text] = []
    for d in state.recent_decisions[-DECISION_LINES:]:
        chain = _chain_label(state, d.get("chain"))
        line = Text()
        if d.get("action") == "entry":
            line.append("ENTRY  ", style=f"bold {GREEN}")
            line.append(f"{chain:<10} {d.get('symbol')}  @ ${d.get('price_usd', 0):.4g}  (${d.get('budget_usd', 0):g})")
        elif d.get("action") == "exit":
            line.append("EXIT   ", style="bold yellow")
            line.append(f"{chain:<10} {d.get('symbol')}  {d.get('reason', '')}")
        elif d.get("action") == "entry_skipped":
            line.append("skip   ", style=DIM)
            line.append(f"{d.get('symbol')}  {d.get('reason', '')}", style=DIM)
        else:
            continue
        lines.append(line)
    for _ in range(DECISION_LINES - len(lines)):
        lines.append(Text(""))
    if not state.recent_decisions:
        lines[0] = Text("(no entries or exits yet)", style=DIM)
    grid = Table.grid(expand=True)
    grid.add_column()
    for line in lines:
        grid.add_row(line)
    return Panel(grid, title="Recent decisions", title_align="left", border_style="magenta")


def render(state: DashboardState) -> Group:
    return Group(_header(state), _reasons_table(state), _positions_table(state), _recent_panel(state))


def make_console() -> Console:
    return Console()
