"""`python -m sniper <command>`: collect, watch, web, backtest-bouncer, leaderboard, profile, enrich, report, money."""
import argparse
import asyncio
import json
from pathlib import Path

from . import analyze, collect, report
from .config import DB_PATH, REPORTS_DIR, SniperConfig
from .profile import enrich_launches, profile_many
from .store import Store


def _load(store, cfg):
    since = __import__('time').time() - cfg.analysis_days * 86400 if cfg.analysis_days else None
    data = store.load_for_analysis(cfg.snipe_s, since=since)
    pools, trades = analyze.prepare(data['pools'], data['trades'], cfg, since=data.get('since') or None)
    return data, pools, trades


def _cfg(args) -> SniperConfig:
    cfg = SniperConfig.load(Path(args.config) if args.config else None)
    if getattr(args, "chains", None):
        cfg.chains = [c.strip() for c in args.chains.split(",") if c.strip()]
    for name in ("snipe_s", "min_launches", "interval_s"):
        value = getattr(args, name, None)
        if value is not None:
            setattr(cfg, name, value)
    return cfg


def cmd_collect(args):
    cfg = _cfg(args)
    try:
        result = asyncio.run(collect.run(cfg, Path(args.db), minutes=args.minutes, max_credits=args.max_credits))
    except KeyboardInterrupt:
        print("\nstopped. the database keeps everything recorded so far; run it again to resume.")
        return
    if result == "budget":
        raise SystemExit(3)  # scripts/run-forever.cmd treats exit code 3 as "do not restart"
    if result == "locked":
        raise SystemExit(4)


def cmd_watch(args):
    from . import ui

    ui.watch(Path(args.db), _cfg(args), replay=args.replay)


def cmd_leaderboard(args):
    cfg = _cfg(args)
    store = Store(Path(args.db))
    data, pools, trades = _load(store, cfg)
    stats = analyze.wallet_stats(trades, pools, cfg, info=data["info"])
    for label in analyze.SERIAL_CLASSES:
        rows = sorted((s for s in stats.values() if s["label"] == label), key=lambda s: (-s["launches"], s["median_sec_offset"]))
        if not rows:
            continue
        print(f"\n{label} ({len(rows)})")
        print(f"{'wallet':44} {'launches':>8} {'creator':>7} {'speed':>7} {'median $':>9} {'total $':>10} {'sold':>5} {'trips':>5} {'hold':>7}")
        for s in rows[: args.top]:
            hold = "-" if s["median_hold_in_window_s"] is None else f"{s['median_hold_in_window_s']:.0f}s"
            print(f"{s['wallet']:44} {s['launches']:>8} {s['launcher_launches']:>7} {'+' + format(s['median_sec_offset'], '.0f') + 's':>7} {s['median_snipe_usd']:>9,.2f} {s['snipe_usd']:>10,.2f} {s['sold_in_window']:>5} {s['round_trips']:>5} {hold:>7}")
    packs = analyze.find_packs(trades, stats, cfg)
    if packs:
        print(f"\n{len(packs)} pack(s):")
        for p in packs[:10]:
            print(f"  {p['size']} {p['kind']}s, {p['shared_launches']} shared launches, same block {100 * p['same_block_rate']:.0f}%: {', '.join(w[:10] for w in p['members'][:8])}")


def cmd_profile(args):
    cfg = _cfg(args)
    store = Store(Path(args.db))
    data, pools, trades = _load(store, cfg)
    stats = analyze.wallet_stats(trades, pools, cfg, info=data["info"])
    rows = []
    for label in ("serial_sniper", "round_tripper"):
        rows += sorted((s for s in stats.values() if s["label"] == label), key=lambda s: -s["launches"])[: args.top]
    token_of_pool = {f"{p['chain']}:{p['pool']}": p["token"] for p in store.all_pools()}
    profiles, credits = asyncio.run(profile_many(rows, cfg.chains, token_of_pool))
    for prof in profiles:
        store.save_profile(prof["wallet"], prof)
    store.commit()
    print(f"profiled {len(profiles)} wallets, {credits} credits")


def cmd_enrich(args):
    """token_info for launches that drew serial wallets and haven't been enriched yet."""
    cfg = _cfg(args)
    store = Store(Path(args.db))
    data, pools, trades = _load(store, cfg)
    done = data["info"]
    stats = analyze.wallet_stats(trades, pools, cfg, info=done)
    serial = analyze.serial_wallets(stats)
    touched = {(t["chain"], t["pool"]) for t in trades if t["wallet"] in serial}
    todo = [p for p in pools if (p["chain"], p["pool"]) in touched and (p["chain"], p["pool"]) not in done][: args.max_launches]
    results, credits = asyncio.run(enrich_launches(todo))
    for r in results:
        if "error" not in r:
            store.save_launch_info(r["chain"], r["pool"], r)
    store.commit()
    print(f"enriched {sum(1 for r in results if 'error' not in r)} of {len(todo)} launches, {credits} credits")


def cmd_money(args):
    from . import money

    cfg = _cfg(args)
    store = Store(Path(args.db))
    summary = money.run(store, cfg)
    print(money.fmt(summary))


def cmd_backtest_bouncer(args):
    import json as _json

    from . import backtest_bouncer

    cfg = _cfg(args)
    store = Store(Path(args.db))
    res = backtest_bouncer.run(store, cfg, fetch=not args.no_fetch)
    print(backtest_bouncer.fmt(res))
    if args.out:
        Path(args.out).write_text(_json.dumps(res, default=str), encoding="utf-8")
        print(f"rows -> {args.out}")


def cmd_web(args):
    from . import web

    web.serve(Path(args.db), _cfg(args), port=args.port)


def cmd_report(args):
    cfg = _cfg(args)
    store = Store(Path(args.db))
    text, summary = report.build(store, cfg, top=args.top, handle=args.handle)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = Path(args.out) if args.out else REPORTS_DIR / "serial-snipers.md"
    out.write_text(text, encoding="utf-8")
    print(json.dumps(summary))
    print(f"report -> {out}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m sniper", description="New Pair Bouncer: pre-entry checks for new pairs, on CoinGecko API data")
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--config", default=None, help="path to a sniper.yaml (default: repo root)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("collect", help="record launch tapes and outcomes (runs until stopped, or --minutes)")
    c.add_argument("--chains", help="comma-separated GeckoTerminal network ids, e.g. robinhood")
    c.add_argument("--minutes", type=float, default=None)
    c.add_argument("--interval-s", dest="interval_s", type=int, default=None)
    c.add_argument("--max-credits", type=int, default=None, help="stop before spending more than this many credits")
    c.set_defaults(fn=cmd_collect)

    w = sub.add_parser("watch", help="live colored feed of the running collector's verdicts and paper exits (read-only, 0 credits)")
    w.add_argument("--replay", type=int, default=8, help="show the last N verdicts first")
    w.set_defaults(fn=cmd_watch)

    commands = (
        ("leaderboard", cmd_leaderboard, "print serial wallets by class, and packs"),
        ("profile", cmd_profile, "enrich top serial snipers and round-trippers with wallet PnL and trade history"),
        ("enrich", cmd_enrich, "fetch developer / launchpad info for launches that drew serial wallets"),
        ("report", cmd_report, "write the markdown report"),
        ("money", cmd_money, "what serial wallets put into their snipes and took out (CoinGecko wallet PnL), stored for the report and dashboard"),
        ("web", cmd_web, "live dashboard on http://localhost:8765 (read-only, run it next to the collector)"),
        ("backtest-bouncer", cmd_backtest_bouncer, "walk-forward replay of the pre-entry checks over recorded launches, vs their +60m outcome"),
    )
    for name, fn, help_ in commands:
        p = sub.add_parser(name, help=help_)
        p.add_argument("--chains")
        p.add_argument("--top", type=int, default=20)
        p.add_argument("--snipe-s", dest="snipe_s", type=int, default=None)
        p.add_argument("--min-launches", dest="min_launches", type=int, default=None)
        if name == "enrich":
            p.add_argument("--max-launches", type=int, default=500)
        if name == "report":
            p.add_argument("--handle", default=None)
            p.add_argument("--out", default=None)
        if name == "web":
            p.add_argument("--port", type=int, default=8765)
        if name == "backtest-bouncer":
            p.add_argument("--out", default=None, help="write every launch's verdict and outcome to this JSON file")
            p.add_argument("--no-fetch", action="store_true", help="don't fetch missing minute candles (1 API call per launch, cached)")
        p.set_defaults(fn=fn)

    args = ap.parse_args(argv)
    args.fn(args)
