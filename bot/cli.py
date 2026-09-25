"""`python -m bot <command>`: run, backtest, report, article-kit, set-link, record, replay, screenshot."""
import argparse
import asyncio
import json
import sys
from pathlib import Path

from core import links as links_mod
from core import report as report_mod
from core.articlekit import build as build_article_kit
from core.client import CoinGeckoClient

from . import backtest as backtest_mod
from . import config, runstore
from .networks import UnknownNetworkError
from .record import record_session
from .replay import replay_file
from .screenshot import screenshot_run
from .strategy import Strategy


def _load_strategy(name: str) -> Strategy:
    path = Path(config.STRATEGIES_DIR) / f"{name}.yaml"
    if not path.exists():
        available = ", ".join(p.stem for p in Path(config.STRATEGIES_DIR).glob("*.yaml"))
        print(f"no such strategy '{name}'. available: {available}", file=sys.stderr)
        raise SystemExit(1)
    return Strategy.load(path)


def cmd_run(args):
    from . import runner

    strategy = _load_strategy(args.strategy)
    mode = "forward" if args.minutes else "autopilot"
    run_dir = runstore.autopilot_run_dir(strategy.name) if mode == "autopilot" else runstore.new_run_dir(mode, strategy.name)
    print(f"[bot] {mode} run: {strategy.name} -> {run_dir} (networks: {', '.join(strategy.networks)})")
    try:
        result = asyncio.run(
            runner.run(
                strategy,
                mode,
                run_dir,
                minutes=args.minutes,
                interval_s=args.interval,
                max_credits_per_day=args.max_credits,
                starting_cash=args.budget,
                use_websocket=not args.no_websocket,
                max_seconds=args.seconds,
            )
        )
    except UnknownNetworkError as exc:
        print(f"[bot] {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(f"\n[bot] run finished. credits_used={result['credits_used']} metrics={result['metrics']}")
    print(f"[bot] run dir: {result['run_dir']}")


def cmd_backtest(args):
    strategy = _load_strategy(args.strategy)
    if args.replay_scans:
        report = backtest_mod.replay_scans(strategy, scans_dir=args.scans_dir)
        run_dir = runstore.new_run_dir("backtest-replay", strategy.name)
        (run_dir / "backtest-report.json").write_text(json.dumps(report, indent=2))
        runstore.write_metrics(run_dir, {"strategy": strategy.name, "mode": "backtest-replay-scans", **report})
        print(json.dumps(report, indent=2))
        print(f"\n[bot] backtest --replay-scans report written to {run_dir}")
    elif args.exits:
        entries = backtest_mod.entries_from_run(args.from_run)
        if not entries:
            print(f"no paper entries found in run '{args.from_run}'. Run `make forward` first, then re-run this.", file=sys.stderr)
            raise SystemExit(1)

        async def go():
            client = CoinGeckoClient()
            try:
                return await backtest_mod.backtest_exits(strategy, client, entries, pages=args.pages)
            finally:
                await client.close()

        report = asyncio.run(go())
        run_dir = runstore.new_run_dir("backtest-exits", strategy.name)
        (run_dir / "backtest-report.json").write_text(json.dumps(report, indent=2))
        runstore.write_metrics(run_dir, {"strategy": strategy.name, "mode": "backtest-exits", **report["metrics"], "positions_tested": report["positions_tested"]})
        print(json.dumps(report, indent=2))
        print(f"\n[bot] backtest --exits report written to {run_dir}")
    else:
        print("pass --replay-scans or --exits", file=sys.stderr)
        raise SystemExit(1)


def cmd_report(args):
    run_dir = runstore.resolve_run(args.run)
    metrics = runstore.read_metrics(run_dir)
    curve = runstore.read_equity_curve(run_dir)
    paths = report_mod.build(run_dir.name, metrics, curve, credits_used=metrics.get("credits_used", 0), out_dir="reports")
    print(f"[bot] report: {paths['html']}")
    print(f"[bot] card:   {paths['card_png']}")


def cmd_article_kit(args):
    run_dir = runstore.resolve_run(args.run)
    metrics = runstore.read_metrics(run_dir)
    curve = runstore.read_equity_curve(run_dir)
    title = args.title or f"My {metrics.get('strategy', 'onchain-signal-bot')} ran while I slept"
    paths = build_article_kit(title, args.handle, metrics, curve, metrics.get("credits_used", 0), out_dir="article-kit")
    if args.dashboard_screenshot:
        shots_dir = Path("article-kit/screenshots")
        shots_dir.mkdir(parents=True, exist_ok=True)
        dest = shots_dir / "dashboard.png"
        screenshot_run(args.run, dest)
        paths["dashboard"] = str(dest)
    print(json.dumps(paths, indent=2))


def cmd_set_link(args):
    files = [p for p in ("README.md", "AGENTS.md") if Path(p).exists()]
    links_mod.set_link(files, args.handle, source=args.source, url_override=args.url)
    print(f"[bot] tagged {', '.join(files)} with utm_source={args.source}&utm_content={args.handle.lstrip('@').lower()}")


def cmd_record(args):
    strategy = _load_strategy(args.strategy)
    try:
        path = asyncio.run(record_session(strategy, scans=args.scans, interval_s=args.interval))
    except UnknownNetworkError as exc:
        print(f"[bot] {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(f"[bot] recorded {args.scans} scans to {path}")


def cmd_replay(args):
    asyncio.run(replay_file(args.file, speed=args.speed))


def cmd_screenshot(args):
    out = screenshot_run(args.run, args.out)
    print(f"[bot] dashboard screenshot: {out}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m bot", description="A 24/7 onchain signal bot on CoinGecko API.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="scan, screen, and paper-trade a strategy. Bounded with --minutes = forward test; unbounded = autopilot.")
    p_run.add_argument("--strategy", required=True)
    p_run.add_argument("--minutes", type=float, default=None, help="stop after N minutes (forward test). Omit to run forever (autopilot).")
    p_run.add_argument("--seconds", type=float, default=None, help="stop autopilot cleanly after N seconds (used by unattended evaluation).")
    p_run.add_argument("--interval", type=float, default=None, help="seconds between scans (default: 60)")
    p_run.add_argument("--max-credits", type=float, default=None, dest="max_credits", help="daily credit budget guard")
    p_run.add_argument("--budget", type=float, default=None, help="starting paper cash (overrides assumptions.yaml)")
    p_run.add_argument("--no-websocket", action="store_true", help="force REST-only position monitoring even on a paid plan")
    p_run.set_defaults(func=cmd_run)

    p_bt = sub.add_parser("backtest", help="replay recorded scans, or test exit rules on historical OHLCV")
    p_bt.add_argument("--strategy", required=True)
    p_bt.add_argument("--replay-scans", action="store_true")
    p_bt.add_argument("--exits", action="store_true")
    p_bt.add_argument("--from-run", default="latest", dest="from_run", help="which run's paper entries to test exits against")
    p_bt.add_argument("--pages", type=int, default=2, help="OHLCV history pages to pull per pool for --exits")
    p_bt.add_argument("--scans-dir", default=config.SCANS_DIR, dest="scans_dir")
    p_bt.set_defaults(func=cmd_backtest)

    p_report = sub.add_parser("report", help="turn a run into report.html + report-card.png")
    p_report.add_argument("--run", default="latest")
    p_report.set_defaults(func=cmd_report)

    p_ak = sub.add_parser("article-kit", help="export cover, report card, equity chart, architecture PNG, and article-draft.md")
    p_ak.add_argument("--run", default="latest")
    p_ak.add_argument("--handle", required=True)
    p_ak.add_argument("--title", default=None)
    p_ak.add_argument("--dashboard-screenshot", action="store_true", dest="dashboard_screenshot", help="also render the terminal dashboard as a PNG into the kit")
    p_ak.set_defaults(func=cmd_article_kit)

    p_link = sub.add_parser("set-link", help="tag every CoinGecko link in README.md/AGENTS.md with utm_source=github&utm_content=<handle>")
    p_link.add_argument("--handle", required=True)
    p_link.add_argument("--source", default="github")
    p_link.add_argument("--url", default=None, help="override the /en/api page link specifically")
    p_link.set_defaults(func=cmd_set_link)

    p_record = sub.add_parser("record", help="capture a short live session to data/recordings/, for free repeat filming")
    p_record.add_argument("--strategy", required=True)
    p_record.add_argument("--scans", type=int, default=3)
    p_record.add_argument("--interval", type=float, default=10.0)
    p_record.set_defaults(func=cmd_record)

    p_replay = sub.add_parser("replay", help="replay a data/recordings/ fixture through the dashboard, at no credit cost")
    p_replay.add_argument("--file", required=True)
    p_replay.add_argument("--speed", type=float, default=1.0)
    p_replay.set_defaults(func=cmd_replay)

    p_shot = sub.add_parser("screenshot", help="render the terminal dashboard for a run to a PNG (needs Playwright)")
    p_shot.add_argument("--run", default="latest")
    p_shot.add_argument("--out", required=True)
    p_shot.set_defaults(func=cmd_screenshot)

    return parser


def main(argv: list[str] | None = None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
