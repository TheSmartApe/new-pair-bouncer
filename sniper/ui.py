"""What `python -m sniper collect` and `python -m sniper watch` print.

In a terminal: a colored live feed, a banner with what the bot knows, one block per verdict with the
reason, the bouncer's paper exits, and a status line per sweep. Redirected to a file (the
supervisor's data/collect.log): the plain timestamped lines, unchanged and greppable.
"""
from __future__ import annotations

import json
import math
import sqlite3
import sys
import time
from pathlib import Path

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

if hasattr(sys.stdout, "reconfigure") and (sys.stdout.encoding or "").lower().replace("-", "") != "utf8":
    try:  # a console stuck on a legacy code page shows '?' for a symbol instead of crashing the bot
        sys.stdout.reconfigure(errors="replace")
    except (OSError, ValueError):
        pass
console = Console(highlight=False)
PRETTY = console.is_terminal

CG_GREEN = "#8dc63f"
RED = "#ef5b50"
AMBER = "#f2b33d"
BADGE = {
    "AVOID": ("bold white on #c4332b", "AVOID"),
    "WATCH": ("bold black on #f2b33d", "WATCH"),
    "ENTER": ("bold black on #8dc63f", "ENTER"),
    "PAPER": ("bold black on #7fb5ff", "PAPER"),
}
LABELS = {
    "rug_ring": "rug ring", "wash_trading": "wash volume", "known_bots": "known bots", "entity_cluster": "cluster",
    "dev_sold": "dev sold", "deployer_rugs": "launch wallet", "serial_launcher": "serial launcher",
    "supply_grab": "supply grab", "top3_supply": "top 3 hold", "buyers": "thin crowd", "liquidity": "liquidity",
    "price_move": "price move", "buy_concentration": "3 wallets", "honeypot": "honeypot", "gt_score": "gt score",
    "token_info": "token info", "dev_holding": "dev holding", "top10_holders": "top 10 hold",
    "fresh_wallets": "fresh wallets", "proven_traders": "proven traders",
}
CHECK_ORDER = "rug ring · crowd · supply grab · wash · bots · clusters · dev · launch wallet · honeypot · wallet pnl"


def _clock(ts: float | None = None) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts or time.time()))


def _usd(x) -> str:
    if x is None:
        return "–"
    x = float(x)
    if abs(x) >= 1e6:
        return f"${x / 1e6:.1f}M"
    if abs(x) >= 1e3:
        return f"${x / 1e3:.1f}k"
    return f"${x:,.0f}"


def _price(p) -> str:
    """$0.00008427 rather than $8.4265277e-05: four significant digits, no exponent."""
    if not p:
        return "$0"
    p = float(p)
    if p >= 1:
        return f"${p:,.4g}" if p < 1e4 else f"${p:,.0f}"
    return f"${p:.{3 - math.floor(math.log10(p))}f}"


def _pct(x) -> str:
    if x is None:
        return "–"
    return f"{'+' if x > 0 else '−' if x < 0 else ''}{abs(x):.1f}%"


def _tone(x) -> str:
    return CG_GREEN if (x or 0) > 0 else RED if (x or 0) < 0 else "white"


def _badge(kind: str) -> Text:
    style, label = BADGE[kind]
    return Text(f" {label} ", style=style)


def _row(ts: float | None, badge: Text, body: Group | Text):
    grid = Table.grid(padding=(0, 1))
    grid.add_column(no_wrap=True, style="dim")
    grid.add_column(no_wrap=True)
    grid.add_column(ratio=1)
    grid.add_row(_clock(ts), badge, body)
    console.print(grid)


# ---- plain log lines, and their colored twin ----


def log(msg: str):
    """A timestamped line. In a terminal, errors stand out and routine lines stay dim."""
    if not PRETTY:
        print(f"[{_clock()}] {msg}", flush=True)
        return
    low = msg.lower()
    style = RED if ("error" in low or "traceback" in low) else AMBER if any(w in low for w in ("failed", "retry", "pausing", "already running", "stopping")) else "dim"
    console.print(Text(_clock(), style="dim") + Text("  ") + Text(msg, style=style))


# ---- banner ----


def memory_stats(db, cfg) -> dict:
    """What the bot knows, for the banner. Plain SQL on the bot's own tables, no API call."""
    b = cfg.bouncer_config()
    one = lambda sql, *a: db.execute(sql, a).fetchone()[0]  # noqa: E731
    classes = {r[0]: r[1] for r in db.execute("SELECT label, COUNT(*) FROM wallet_classes GROUP BY label")}
    ring = one(
        """WITH wp AS (SELECT DISTINCT chain, pool, wallet FROM trades WHERE kind = 'buy'),
                o AS (SELECT chain, pool, CASE WHEN trades_m30 = 0 AND COALESCE(reserve_usd, 0) < ? THEN 1 ELSE 0 END AS rug
                      FROM snapshots WHERE target_age_min = 60 AND trades_m30 IS NOT NULL)
           SELECT COUNT(*) FROM (SELECT wp.wallet, COUNT(*) AS n, SUM(o.rug) AS rugs FROM wp JOIN o ON o.chain = wp.chain AND o.pool = wp.pool
                                 GROUP BY wp.wallet HAVING n >= ? AND 1.0 * rugs / n >= ?)""",
        b.rug_reserve_usd, b.ring_min_launches, b.ring_min_rug_share,
    )
    return {
        "wallets": one("SELECT COUNT(DISTINCT wallet) FROM trades"),
        "trades": one("SELECT COUNT(*) FROM trades"),
        "launches": one("SELECT COUNT(*) FROM pools WHERE status = 'captured'"),
        "snipers": classes.get("serial_sniper", 0),
        "bots": classes.get("round_tripper", 0) + classes.get("dust_bot", 0),
        "ring": ring,
    }


def banner(cfg, stats: dict, mode: str = "collect"):
    if not PRETTY:
        return
    b = cfg.bouncer_config()
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim", no_wrap=True)
    grid.add_column()
    lo, hi = math.floor((cfg.window_s + cfg.lag_s) / 60), math.ceil((cfg.window_s + cfg.lag_s + cfg.interval_s) / 60)
    grid.add_row("chain", Text(f"{', '.join(cfg.chains)} · new pairs every {cfg.interval_s}s · verdict {lo}-{hi} min after launch"))
    grid.add_row("memory", Text.assemble(
        (f"{stats['launches']:,}", "bold"), " launches · ", (f"{stats['trades']:,}", "bold"), " launch trades · ",
        (f"{stats['wallets']:,}", "bold"), " wallets"))
    grid.add_row("knows", Text.assemble(
        (f"{stats['ring']:,}", f"bold {RED}"), " rug ring wallets · ", (f"{stats['snipers']:,}", "bold"), " serial snipers · ",
        (f"{stats['bots']:,}", "bold"), " bots"))
    grid.add_row("checks", Text(CHECK_ORDER))
    grid.add_row("paper", Text(f"{_usd(b.position_usd)} a pair · take profit +{b.take_profit_pct:g}% · stop −{b.stop_loss_pct:g}% · out within {b.max_hold_min}m · price impact + fees"))
    grid.add_row("verdicts", Text.assemble(_badge("AVOID"), " a check failed  ", _badge("WATCH"), " 2+ warnings  ", _badge("ENTER"), " all clear"))
    if mode == "watch":
        grid.add_row("mode", Text("watching the running bot · reads its database, spends no credits", style="dim"))
    title = Text.assemble(("NEW PAIR BOUNCER", f"bold {CG_GREEN}"), ("  ·  checks every new pair before it buys", "white"))
    console.print(Panel(grid, title=title, title_align="left", subtitle=Text("data powered by CoinGecko API", style=CG_GREEN),
                        subtitle_align="right", border_style=CG_GREEN, box=box.ROUNDED, padding=(1, 2)))


# ---- one verdict ----


def verdict(name: str, v: dict, ts: float | None = None, snipers: int = 0, snipe_s: int = 10, position_usd: float = 100):
    """One verdict block (terminal only). v is Verdict.to_dict(): {"verdict", "stage", "features", "checks": [...]}."""
    if not PRETTY:
        return
    kind = v["verdict"]
    checks = v.get("checks", [])
    f = v.get("features") or {}
    fails = sorted((c for c in checks if c["status"] == "fail"), key=lambda c: c["key"] != "rug_ring")
    warns = [c for c in checks if c["status"] == "warn"]
    head = Text.assemble((name, "bold white"), "   ")
    facts = []
    if f.get("buyers") is not None:
        facts.append(f"{f['buyers']} buyer{'' if f['buyers'] == 1 else 's'}")
    if f.get("volume_usd"):
        facts.append(f"{_usd(f['volume_usd'])} volume")
    if f.get("reserve_usd") is not None:
        facts.append(f"{_usd(f['reserve_usd'])} liquidity")
    head.append(" · ".join(facts), style="dim")
    lines = [head]
    if kind == "AVOID":
        top = fails[0]
        lines.append(Text.assemble((f"{LABELS.get(top['key'], top['key'])}  ", f"bold {RED}"), (top["reason"], "white")))
        if len(fails) > 1:
            lines.append(Text("also failed: " + " · ".join(LABELS.get(c["key"], c["key"]) for c in fails[1:]), style="dim"))
    elif kind == "WATCH":
        lines.append(Text.assemble((f"{len(warns)} warnings  ", f"bold {AMBER}"), (warns[0]["reason"], "white")))
        lines.append(Text("also: " + " · ".join(LABELS.get(c["key"], c["key"]) for c in warns[1:]), style="dim"))
    else:
        passed = sum(1 for c in checks if c["status"] == "pass")
        msg = f"passed {passed} checks · "
        msg += f"paper buy {_usd(position_usd)} at {_price(f.get('price_usd'))}" if f.get("fillable", True) else "pool too thin to fill, no paper buy"
        lines.append(Text(msg, style=f"bold {CG_GREEN}"))
    if snipers:
        lines.append(Text(f"◆ {snipers} known serial sniper{'' if snipers == 1 else 's'} bought in the first {snipe_s}s", style="#7fb5ff"))
    _row(ts, _badge(kind), Group(*lines))


def snipers_alert(name: str, n: int, snipe_s: int):
    """A serial-wallet alert on a pair that got no verdict (bouncer off or checks failed). Terminal only."""
    if not PRETTY:
        return
    _row(None, Text(" ALERT ", style="bold black on #7fb5ff"), Text.assemble((name, "bold white"), (f"   {n} known serial sniper{'' if n == 1 else 's'} bought in the first {snipe_s}s", "#7fb5ff")))


def paper_exit(name: str, e: dict, ts: float | None = None):
    """A bouncer-book position closing: e = {book, pnl, usd, reason, opened_ts}."""
    if not PRETTY:
        return
    pct = 100 * e["pnl"] / e["usd"] if e.get("usd") else None
    held = f" · held {((ts or time.time()) - e['opened_ts']) / 60:.0f} min" if e.get("opened_ts") is not None else ""
    body = Text.assemble((name, "bold white"), "   closed ", (_pct(pct), f"bold {_tone(pct)}"),
                         (f" ({'+' if e['pnl'] >= 0 else '−'}${abs(e['pnl']):.2f})", _tone(pct)),
                         (f" · {e['reason'].replace('_', ' ')}{held}", "dim"))
    _row(ts, _badge("PAPER"), body)


# ---- status line ----


def books_line(paper_rows: list[dict], b) -> Text:
    from .bouncer import book_stats

    t = Text("paper  ", style="dim")
    for i, (book, label) in enumerate((("bouncer", "bouncer"), ("control", "buy everything"))):
        s = book_stats(paper_rows, book, b)
        if i:
            t.append("  ·  ", style="dim")
        t.append(f"{label} ", style="white" if book == "bouncer" else "dim")
        t.append(_pct(s["return_pct"]) if s["positions"] else "–", style=f"bold {_tone(s['return_pct'])}" if s["positions"] else "dim")
        t.append(f" ({s['positions']} trades, {s['open']} open)", style="dim")
    return t


def sweep(chain: str, n: int, found: dict, c: dict, credits: float, seconds: float, books: Text | None = None):
    """The status block after each sweep (terminal only)."""
    if not PRETTY:
        return
    checked = c["ENTER"] + c["WATCH"] + c["AVOID"]
    title = Text.assemble((f" {_clock()} · sweep {n} · {chain} ", "dim"))
    console.print()
    console.rule(title, style="#3d5145", align="left")
    t = Text.assemble(
        (f"+{found['new']} new pairs", "bold"), "   ", (f"{checked} checked: ", "dim"),
        (f"{c['AVOID']} avoid", RED), (" · ", "dim"), (f"{c['WATCH']} watch", AMBER), (" · ", "dim"), (f"{c['ENTER']} enter", CG_GREEN),
        ("   ", ""), (f"{credits:,.0f} credits today · {seconds:.0f}s", "dim"),
    )
    console.print(t)
    if books is not None:
        console.print(books)


# ---- `python -m sniper watch`: the live feed of a bot that's already running ----


def watch(db_path: Path, cfg, replay: int = 8, poll_s: float = 2.0):
    if not PRETTY:
        raise SystemExit("`watch` is a live terminal view: run it in a terminal (data/collect.log has the plain log)")
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    b = cfg.bouncer_config()
    banner(cfg, memory_stats(db, cfg), mode="watch")
    names: dict[tuple, str] = {}

    def name_of(chain, pool):
        if (chain, pool) not in names:
            row = db.execute("SELECT name FROM pools WHERE chain=? AND pool=?", (chain, pool)).fetchone()
            names[(chain, pool)] = (row[0] if row else None) or pool[:10]
        return names[(chain, pool)]

    def show(rows):
        for r in rows:
            v = json.loads(r["checks_json"])
            verdict(name_of(r["chain"], r["pool"]), v, ts=r["decided_ts"], position_usd=b.position_usd)

    rows = list(db.execute("SELECT * FROM verdicts ORDER BY decided_ts DESC LIMIT ?", (replay,)))[::-1]
    if rows:
        console.print(Text(f"last {len(rows)} verdicts", style="dim"))
    show(rows)
    last_v = rows[-1]["decided_ts"] if rows else time.time()
    last_x = db.execute("SELECT COALESCE(MAX(closed_ts), 0) FROM paper").fetchone()[0]
    last_status = 0.0
    try:
        while True:
            new = list(db.execute("SELECT * FROM verdicts WHERE decided_ts > ? ORDER BY decided_ts", (last_v,)))
            if new:
                show(new)
                last_v = new[-1]["decided_ts"]
            for x in db.execute("SELECT * FROM paper WHERE book='bouncer' AND closed_ts > ? ORDER BY closed_ts", (last_x,)):
                paper_exit(name_of(x["chain"], x["pool"]), {"pnl": x["pnl_usd"] or 0, "usd": x["usd"], "reason": x["exit_reason"] or "", "opened_ts": x["opened_ts"]}, ts=x["closed_ts"])
            last_x = db.execute("SELECT COALESCE(MAX(closed_ts), ?) FROM paper", (last_x,)).fetchone()[0]
            if time.time() - last_status >= 60:
                last_status = time.time()
                _watch_status(db, cfg, b)
            time.sleep(poll_s)
    except KeyboardInterrupt:
        pass
    finally:
        db.close()


def _watch_status(db, cfg, b):
    since = time.time() - 86400
    counts = {k: v for k, v in db.execute("SELECT verdict, COUNT(*) FROM verdicts WHERE decided_ts >= ? GROUP BY verdict", (since,))}
    last_sweep = db.execute("SELECT MAX(seen_ts) FROM pools").fetchone()[0] or 0
    ago = time.time() - last_sweep
    paper = [dict(r) for r in db.execute("SELECT * FROM paper")]
    console.print()
    console.rule(Text(f" {_clock()} · last 24h ", style="dim"), style="#3d5145", align="left")
    t = Text.assemble(
        (f"{sum(counts.values()):,} pairs checked: ", "dim"), (f"{counts.get('AVOID', 0):,} avoid", RED), (" · ", "dim"),
        (f"{counts.get('WATCH', 0):,} watch", AMBER), (" · ", "dim"), (f"{counts.get('ENTER', 0):,} enter", CG_GREEN), "   ",
        (f"bot last swept {ago:.0f}s ago", "dim" if ago < 3 * cfg.interval_s else f"bold {AMBER}"),
    )
    console.print(t)
    console.print(books_line(paper, b))
    if ago >= 3 * cfg.interval_s:
        console.print(Text("the collector doesn't look like it's running: start it with `python -m sniper collect` or scripts/start.ps1", style=AMBER))
