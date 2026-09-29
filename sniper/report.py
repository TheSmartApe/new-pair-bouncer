"""Markdown report: serial snipers, round-trip farms, packs, the devs they serve, and launch outcomes."""
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

from core import links

from . import analyze
from .config import SniperConfig


def _short(w: str | None) -> str:
    return "–" if not w else f"`{w[:8]}…{w[-4:]}`"


def _usd(x) -> str:
    if x is None:
        return "–"
    x = float(x)
    if abs(x) >= 1e6:
        return f"${x / 1e6:,.2f}M"
    if abs(x) >= 1e3:
        return f"${x / 1e3:,.1f}K"
    if abs(x) >= 10:
        return f"${x:,.0f}"
    return f"${x:,.2f}"


def _mult(x) -> str:
    return "–" if x is None else f"{x:.2f}x"


def _secs(s) -> str:
    if s is None:
        return "–"
    return f"{s / 60:.1f} min" if s >= 60 else f"{s:.0f}s"


def _dt(ts) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _outcome_table(L: list[str], outcomes: list[dict], cfg: SniperConfig, field: str, with_label: str, without_label: str):
    L.append("| age | group | launches | alive | median multiple | below snipe price | down 90%+ |")
    L.append("|---|---|---:|---:|---:|---:|---:|")
    for age in cfg.snapshot_ages_min:
        cmp = analyze.compare_outcomes(outcomes, age, field)
        for key, label in (("with", with_label), ("without", without_label)):
            s = cmp[key]
            if not s.get("n"):
                L.append(f"| +{age}m | {label} | 0 | – | – | – | – |")
                continue
            L.append(f"| +{age}m | {label} | {s['n']} | {s['alive_pct']}% | {_mult(s['median_multiple'])} | {s['below_entry_pct']}% | {s['down_90_pct']}% |")
    L.append("")


def build(store, cfg: SniperConfig, top: int = 20, handle: str | None = None) -> tuple[str, dict]:
    pools = store.all_pools()
    trades = store.all_trades()
    snaps = store.all_snapshots()
    stats = analyze.wallet_stats(trades, pools, cfg)
    packs = analyze.find_packs(trades, stats, cfg)
    outcomes = analyze.launch_outcomes(pools, trades, snaps, stats, cfg)
    profiles = store.profiles()
    info = store.launch_info()
    status = Counter(p["status"] for p in pools)
    captured = [p for p in pools if p["status"] == "captured"]
    labels = Counter(s["label"] for s in stats.values())
    hours = analyze.observation_hours(pools)
    snipers = sorted((s for s in stats.values() if s["label"] == "serial_sniper"), key=lambda s: (-s["launches"], s["median_sec_offset"]))
    trippers = sorted((s for s in stats.values() if s["label"] == "round_tripper"), key=lambda s: -s["launches"])
    launchers = sorted((s for s in stats.values() if s["label"] == "serial_launcher"), key=lambda s: -s["launches"])
    n_cap = len(captured) or 1
    with_sniper = sum(1 for o in outcomes if o["serial_snipers"])
    with_tripper = [o for o in outcomes if o["round_trippers"]]
    fake_share = [o["round_tripper_share"] for o in with_tripper]

    L: list[str] = []
    L.append("# Serial Sniper Tracker report")
    L.append("")
    span = ""
    if captured:
        span = f" Launches from {_dt(min(p['created_ts'] for p in captured))} to {_dt(max(p['created_ts'] for p in captured))} ({hours:.1f}h)."
    L.append(f"Generated {_dt(time.time())}. Chains: {', '.join(cfg.chains)}.{span}")
    L.append("")
    L.append("## Headline numbers")
    L.append("")
    L.append(f"- Pools discovered: **{len(pools):,}**. Launch tape recorded: {status.get('captured', 0):,}. Fewer than {cfg.min_buys} buys: {status.get('quiet', 0):,}. No trades in the first {cfg.window_s}s: {status.get('empty', 0):,}.")
    L.append(f"- Wallets that bought within {cfg.snipe_s}s of a launch's first trade: **{len(stats):,}**, of which serial (≥{cfg.min_launches} launches): **{labels.get('serial_sniper', 0) + labels.get('round_tripper', 0) + labels.get('serial_launcher', 0):,}**")
    L.append(f"  - serial snipers (buy early, hold past {cfg.roundtrip_max_s}s): **{labels.get('serial_sniper', 0):,}**")
    L.append(f"  - round-trippers (buy early, sell within {cfg.roundtrip_max_s}s in ≥{cfg.roundtrip_min_rate:.0%} of their launches): **{labels.get('round_tripper', 0):,}**")
    L.append(f"  - serial launch-block buyers (bought in block 0 in most of their launches): **{labels.get('serial_launcher', 0):,}**")
    if labels.get("bundler"):
        L.append(f"  - ERC-4337 bundler addresses set aside: {labels.get('bundler'):,}")
    L.append(f"- Launches with a serial sniper in the first {cfg.snipe_s}s: **{with_sniper:,} of {len(captured):,}** ({100 * with_sniper / n_cap:.0f}%)")
    if with_tripper:
        L.append(f"- Launches with round-trippers in the buyer list: **{len(with_tripper):,} of {len(captured):,}** ({100 * len(with_tripper) / n_cap:.0f}%). In those launches, round-trippers are a median **{100 * sorted(fake_share)[len(fake_share) // 2]:.0f}%** of the wallets that bought in the first {cfg.window_s}s.")
    L.append(f"- Packs (serial wallets sniping the same launches at the same moment): **{len(packs)}**")
    L.append("")

    L.append("## What happened to the launches")
    L.append("")
    L.append("`multiple` = pool price at that age ÷ the average price snipers paid in the snipe window (round-trippers excluded). `alive` = at least one trade in the hour before the snapshot.")
    L.append("")
    L.append("**Serial snipers present vs not**")
    L.append("")
    _outcome_table(L, outcomes, cfg, "serial_snipers", "serial sniper present", "no serial sniper")
    L.append("**Round-trippers present vs not**")
    L.append("")
    _outcome_table(L, outcomes, cfg, "round_trippers", "round-trippers present", "no round-trippers")

    L.append(f"## Serial snipers (top {min(top, len(snipers))})")
    L.append("")
    L.append("Wallets that keep buying in the first seconds of launches and hold past the round-trip window.")
    L.append("")
    L.append("| wallet | launches | median speed | median buy | total sniped | sold within window | launches alive at +60m | median multiple at +60m | CoinGecko realized PnL | tokens traded | FIFO median hold on snipes |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for s in snipers[:top]:
        tr = analyze.sniper_track_record(s["wallet"], outcomes, 60)
        prof = profiles.get(s["wallet"], {})
        cg = prof.get("coingecko_pnl") or {}
        alive = "–" if tr["alive_pct"] is None else f"{tr['alive_pct']:.0f}% of {tr['launches_with_outcome']}"
        L.append(
            f"| {_short(s['wallet'])} | {s['launches']} | +{s['median_sec_offset']:.0f}s / +{s['median_block_offset']:.0f} blk | {_usd(s['median_snipe_usd'])} | {_usd(s['snipe_usd'])} | {s['sold_in_window']} | {alive} | {_mult(tr['median_multiple'])} | {_usd(cg.get('total_realized_pnl_usd'))} | {cg.get('total_tokens') or '–'} | {_secs((prof.get('snipes') or {}).get('median_hold_s'))} |"
        )
    L.append("")

    if trippers:
        L.append(f"## Round-trippers (top {min(top, len(trippers))})")
        L.append("")
        L.append(f"Wallets that buy in the first seconds and sell again within {cfg.roundtrip_max_s}s, launch after launch. Paying a small loss every time to appear in a new token's buyer list is consistent with activity or volume bots. That reading is an inference from the trades, not a CoinGecko label.")
        L.append("")
        L.append("| wallet | launches | round trips | median hold | median buy | total bought | net cost of round trips | CoinGecko realized PnL | tokens traded |")
        L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for s in trippers[:top]:
            cg = (profiles.get(s["wallet"], {}).get("coingecko_pnl")) or {}
            L.append(
                f"| {_short(s['wallet'])} | {s['launches']} | {s['round_trips']} | {_secs(s['median_hold_in_window_s'])} | {_usd(s['median_snipe_usd'])} | {_usd(s['snipe_usd'])} | {_usd(s['round_trip_cost_usd'])} | {_usd(cg.get('total_realized_pnl_usd'))} | {cg.get('total_tokens') or '–'} |"
            )
        L.append("")

    if launchers:
        L.append(f"## Serial launch-block buyers (top {min(top, len(launchers))})")
        L.append("")
        L.append("Wallets that bought in the launch block itself in most of their launches. On launchpads this is usually the token's developer.")
        L.append("")
        L.append("| wallet | launches | in launch block | median buy | sold within window | median hold | round trips |")
        L.append("|---|---:|---:|---:|---:|---:|---:|")
        for s in launchers[:top]:
            L.append(f"| {_short(s['wallet'])} | {s['launches']} | {s['block0_launches']} | {_usd(s['median_snipe_usd'])} | {s['sold_in_window']} | {_secs(s['median_hold_in_window_s'])} | {s['round_trips']} |")
        L.append("")

    if packs:
        L.append("## Packs")
        L.append("")
        L.append(f"Serial wallets linked because they sniped the same launches (Jaccard ≥ {cfg.pack_min_overlap}, ≥ {cfg.pack_min_shared} shared) within a median {cfg.pack_max_block_gap} blocks of each other. `same block` = share of shared launches where two or more members bought in the very same block.")
        L.append("")
        for i, p in enumerate(packs[:10], 1):
            L.append(f"**Pack {i}** ({p['kind'].replace('_', ' ')}s): {p['size']} wallets, {p['shared_launches']} launches together, same block {100 * p['same_block_rate']:.0f}% of the time, {_usd(p['snipe_usd'])} bought, {_usd(p['round_trip_cost_usd'])} net cost of round trips")
            L.append("")
            L.append("- " + ", ".join(_short(w) for w in p["members"][:12]) + (" …" if p["size"] > 12 else ""))
            L.append("")

    if info and with_tripper:
        by_dev: dict[str, list[dict]] = defaultdict(list)
        for o in with_tripper:
            i = info.get((o["chain"], o["pool"]))
            if i and i.get("developer"):
                by_dev[i["developer"]].append(o)
        if by_dev:
            L.append("## Developers whose launches drew round-trippers")
            L.append("")
            L.append("From token info `developer_address` (launchpad tokens only).")
            L.append("")
            L.append("| developer | launches with round-trippers | tickers | median round-tripper share of buyers |")
            L.append("|---|---:|---|---:|")
            for dev, rows in sorted(by_dev.items(), key=lambda kv: -len(kv[1]))[:15]:
                tickers = Counter((r["name"] or "?").split(" / ")[0] for r in rows)
                shares = sorted(r["round_tripper_share"] for r in rows)
                L.append(f"| {_short(dev)} | {len(rows)} | {', '.join(f'{t}×{n}' if n > 1 else t for t, n in tickers.most_common(6))} | {100 * shares[len(shares) // 2]:.0f}% |")
            L.append("")

    L.append("## Method and caveats")
    L.append("")
    L.append(f"- Data: CoinGecko API onchain endpoints only (new pools, pool trades by time range, pools multi, token info, wallet PnL, wallet trades). A launch tape is every trade in the first {cfg.window_s}s after pool creation; offsets are measured from the pool's first indexed trade.")
    L.append(f"- Every class here is computed by this repo, not supplied by CoinGecko: *snipe* = a buy within {cfg.snipe_s}s of the first trade; *serial* = ≥{cfg.min_launches} sniped launches; *round trip* = a snipe sold within {cfg.roundtrip_max_s}s; *round-tripper* = round trips in ≥{cfg.roundtrip_min_rate:.0%} of a wallet's launches; *launch-block buyer* = bought in block 0 in most launches; *pack* as defined above.")
    L.append(f"- Trades are attributed to the transaction sender. Addresses starting with {', '.join(cfg.bundler_prefixes)} are ERC-4337 bundlers submitting other users' trades and are set aside, so smart-account snipers routed through a bundler are not counted.")
    L.append("- The tracker only sees launches while it runs, and wallet trade history reaches back about a week. Multiples compare a later pool price with the snipe-window average price; they are not anyone's realized PnL.")
    L.append("- Research, not trading advice. Nothing here places trades.")
    L.append("")
    L.append("## Links")
    L.append("")
    L.append("- CoinGecko API: https://www.coingecko.com/en/api")
    L.append("- Pricing: https://www.coingecko.com/en/api/pricing")
    L.append("- Docs: https://docs.coingecko.com")
    L.append("")
    text = links.rewrite_text("\n".join(L), handle or cfg.handle or "serial-sniper-tracker")
    summary = {
        "pools": len(pools),
        "captured": len(captured),
        "hours": round(hours, 2),
        "labels": dict(labels),
        "packs": len(packs),
        "launches_with_serial_sniper": with_sniper,
        "launches_with_round_trippers": len(with_tripper),
    }
    return text, summary
