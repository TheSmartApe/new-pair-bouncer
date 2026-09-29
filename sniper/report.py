"""Markdown report: serial snipers, round-trip bots, packs, the devs they serve, and launch outcomes."""
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


def _dt(ts, fmt="%Y-%m-%d %H:%M UTC") -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(fmt)


def _pct(a, b) -> str:
    return "–" if not b else f"{100 * a / b:.0f}%"


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


def window_summary(all_pools, all_trades, snaps, info, cfg, since, until, buyer_counts=None) -> dict:
    """Stats for launches created in [since, until), with wallet classes computed from that window
    alone, so a day's numbers don't depend on how long the tracker had been running before it."""
    pools, trades = analyze.prepare(all_pools, all_trades, cfg, since=since, until=until)
    stats = analyze.wallet_stats(trades, pools, cfg, info=info)
    outcomes = analyze.launch_outcomes(pools, trades, snaps, stats, cfg, buyer_counts)
    labels = Counter(s["label"] for s in stats.values())
    n = len(pools)
    out = {
        "launches": n,
        "hours": analyze.observation_hours(pools),
        "labels": labels,
        "with_sniper": sum(1 for o in outcomes if o["serial_snipers"]),
        "with_tripper": sum(1 for o in outcomes if o["round_trippers"]),
        "outcomes": outcomes,
    }
    first = analyze.compare_outcomes(outcomes, cfg.snapshot_ages_min[0], "serial_snipers") if cfg.snapshot_ages_min else None
    out["alive_first_age"] = first
    return out


def build(store, cfg: SniperConfig, top: int = 20, handle: str | None = None, data: dict | None = None) -> tuple[str, dict]:
    if data is None:
        since = time.time() - cfg.analysis_days * 86400 if cfg.analysis_days else None
        data = store.load_for_analysis(cfg.snipe_s, since=since)
    all_pools, all_trades, snaps, info = data["pools"], data["trades"], data["snapshots"], data["info"]
    buyer_counts = data.get("buyer_counts")
    horizon = data.get("since") or None
    profiles = store.profiles()
    pools, trades = analyze.prepare(all_pools, all_trades, cfg, since=horizon)
    stats = analyze.wallet_stats(trades, pools, cfg, info=info)
    packs = analyze.find_packs(trades, stats, cfg)
    outcomes = analyze.launch_outcomes(pools, trades, snaps, stats, cfg, buyer_counts)
    in_scope = [p for p in all_pools if horizon is None or p["created_ts"] >= horizon]
    status = Counter(p["status"] for p in in_scope)
    truncated = sum(1 for p in in_scope if p["status"] == "captured" and p.get("truncated"))
    extra = status.get("secondary", 0) + status.get("captured", 0) - truncated - len(pools)
    labels = Counter(s["label"] for s in stats.values())
    by_label = {lab: sorted((s for s in stats.values() if s["label"] == lab), key=lambda s: (-s["launches"], s["median_sec_offset"])) for lab in analyze.SERIAL_CLASSES}
    now = time.time()
    first_age = cfg.snapshot_ages_min[0] if cfg.snapshot_ages_min else None

    L: list[str] = []
    L.append("# Serial Sniper Tracker report")
    L.append("")
    span = ""
    if pools:
        span = f" Launches from {_dt(min(p['created_ts'] for p in pools))} to {_dt(max(p['created_ts'] for p in pools))}."
    L.append(f"Generated {_dt(now)}. Chains: {', '.join(cfg.chains)}.{span}")
    L.append("")

    # ---- last 24h, classes computed on that window only ----
    last = window_summary(all_pools, all_trades, snaps, info, cfg, now - 86400, None, buyer_counts)
    ll = last["labels"]
    partial = f" (the tracker has only {last['hours']:.1f}h of launches in this window)" if last["hours"] < 23 else ""
    L.append(f"## Last 24 hours{partial}")
    L.append("")
    L.append(f"Wallet classes below are computed from these 24 hours alone, so the numbers compare day to day.")
    L.append("")
    L.append(f"- Launches analyzed: **{last['launches']:,}** (each token's first pool, complete launch tape)")
    L.append(f"- Launches with a serial sniper in the first {cfg.snipe_s}s: **{last['with_sniper']:,}** ({_pct(last['with_sniper'], last['launches'])})")
    L.append(f"- Launches with round-trip bots in the buyer list: **{last['with_tripper']:,}** ({_pct(last['with_tripper'], last['launches'])})")
    L.append(f"- Serial snipers: **{ll.get('serial_sniper', 0):,}**, round-trippers: **{ll.get('round_tripper', 0):,}**, dust bots: **{ll.get('dust_bot', 0):,}**, serial launchers: **{ll.get('serial_launcher', 0):,}**")
    L.append("")

    # ---- per day ----
    days = analyze.daily_windows(pools)
    if len(days) > 1 or (days and last["hours"] >= 23):
        L.append("## Day by day")
        L.append("")
        hdr = "| day (UTC) | launches | with serial sniper | with round-trippers | serial snipers | round-trippers | dust bots | launchers |"
        sep = "|---|---:|---:|---:|---:|---:|---:|---:|"
        if first_age:
            hdr += f" alive at +{first_age}m: with sniper / without |"
            sep += "---:|"
        L.append(hdr)
        L.append(sep)
        for since, until in days:
            w = window_summary(all_pools, all_trades, snaps, info, cfg, since, until, buyer_counts)
            lab = w["labels"]
            row = f"| {_dt(since, '%Y-%m-%d')} | {w['launches']:,} | {_pct(w['with_sniper'], w['launches'])} | {_pct(w['with_tripper'], w['launches'])} | {lab.get('serial_sniper', 0)} | {lab.get('round_tripper', 0)} | {lab.get('dust_bot', 0)} | {lab.get('serial_launcher', 0)} |"
            if first_age:
                a = w["alive_first_age"]
                wa = f"{a['with']['alive_pct']}%" if a["with"].get("n") else "–"
                wo = f"{a['without']['alive_pct']}%" if a["without"].get("n") else "–"
                row += f" {wa} / {wo} |"
            L.append(row)
        L.append("")

    # ---- whole run ----
    scope = f"last {cfg.analysis_days} days" if horizon else "whole run"
    L.append(f"## Whole run ({scope})" if horizon else "## Whole run")
    L.append("")
    L.append(f"- Pools discovered: **{len(in_scope):,}**. Launches analyzed: **{len(pools):,}**. Left out: {status.get('quiet', 0):,} with fewer than {cfg.min_buys} buys, {status.get('empty', 0):,} with no trades, {extra:,} extra pools of a token that already had one, {truncated:,} tapes cut off by the page cap, {status.get('expired', 0):,} never captured in time, {status.get('pending', 0):,} still pending.")
    L.append(f"- Wallets that sniped at least once (bought within {cfg.snipe_s}s of a launch's first trade): **{len(stats):,}**")
    L.append(f"  - serial snipers (≥{cfg.min_launches} launches, buy early and hold or sell at a profit): **{labels.get('serial_sniper', 0):,}**")
    L.append(f"  - round-trippers (sell within {cfg.roundtrip_max_s}s in ≥{cfg.roundtrip_min_rate:.0%} of launches, or on a fixed timer, without making money): **{labels.get('round_tripper', 0):,}**")
    L.append(f"  - dust bots (median snipe under ${cfg.dust_usd:g}): **{labels.get('dust_bot', 0):,}**")
    L.append(f"  - serial launchers (the developer, or the creation-block buyer, in most of their launches): **{labels.get('serial_launcher', 0):,}**")
    if labels.get("bundler"):
        L.append(f"  - ERC-4337 bundler addresses set aside: {labels.get('bundler'):,}")
    L.append(f"- Packs (serial wallets sniping the same launches at the same moment): **{len(packs)}**")
    L.append("")

    L.append(f"## What happened to the launches ({scope})")
    L.append("")
    L.append("`multiple` = pool price at that age ÷ the average price paid in the snipe window by everyone except bots and launchers. `alive` = at least one trade in the 30 minutes (up to +2h) or the hour (later ages) before the snapshot. Snapshots taken late, e.g. after the collector was down, are left out.")
    L.append("")
    L.append("**Serial snipers present vs not**")
    L.append("")
    _outcome_table(L, outcomes, cfg, "serial_snipers", "serial sniper present", "no serial sniper")
    L.append("**Round-trippers present vs not**")
    L.append("")
    _outcome_table(L, outcomes, cfg, "round_trippers", "round-trippers present", "no round-trippers")

    snipers = by_label["serial_sniper"]
    L.append(f"## Serial snipers (top {min(top, len(snipers))})")
    L.append("")
    L.append("Wallets that keep buying in the first seconds of launches and either hold past the round-trip window or sell at a profit. PnL columns come from CoinGecko's wallet PnL endpoint: on the tokens this tracker saw the wallet snipe, and on all tokens excluding ETH/WETH.")
    L.append("")
    L.append("| wallet | launches | median speed | median buy | total sniped | sold within window | launches alive at +60m | median multiple at +60m | realized PnL on sniped tokens | realized PnL excl. ETH | FIFO median hold on snipes |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for s in snipers[:top]:
        tr = analyze.sniper_track_record(s["wallet"], outcomes, 60)
        prof = profiles.get(s["wallet"], {})
        cg = prof.get("coingecko_pnl") or {}
        alive = "–" if tr["alive_pct"] is None else f"{tr['alive_pct']:.0f}% of {tr['launches_with_outcome']}"
        L.append(
            f"| {_short(s['wallet'])} | {s['launches']} | +{s['median_sec_offset']:.0f}s / +{s['median_block_offset']:.0f} blk | {_usd(s['median_snipe_usd'])} | {_usd(s['snipe_usd'])} | {s['sold_in_window']} | {alive} | {_mult(tr['median_multiple'])} | {_usd(cg.get('realized_on_sniped_tokens_usd'))} | {_usd(cg.get('realized_excl_eth_usd'))} | {_secs((prof.get('snipes') or {}).get('median_hold_s'))} |"
        )
    L.append("")

    trippers = by_label["round_tripper"]
    if trippers:
        L.append(f"## Round-trippers (top {min(top, len(trippers))})")
        L.append("")
        L.append(f"Wallets that buy in the first seconds and sell again within {cfg.roundtrip_max_s}s (or on a fixed timer), launch after launch, without making money on it. Paying a small loss every time to appear in a new token's buyer list is consistent with activity or volume bots. That reading is an inference from the trades, not a CoinGecko label.")
        L.append("")
        L.append("| wallet | launches | round trips | timer seller | median hold | median buy | total bought | net cost of round trips | realized PnL on sniped tokens |")
        L.append("|---|---:|---:|:---:|---:|---:|---:|---:|---:|")
        for s in trippers[:top]:
            cg = (profiles.get(s["wallet"], {}).get("coingecko_pnl")) or {}
            L.append(
                f"| {_short(s['wallet'])} | {s['launches']} | {s['round_trips']} | {'yes' if s['timer_seller'] else ''} | {_secs(s['median_hold_in_window_s'])} | {_usd(s['median_snipe_usd'])} | {_usd(s['snipe_usd'])} | {_usd(s['round_trip_cost_usd'])} | {_usd(cg.get('realized_on_sniped_tokens_usd'))} |"
            )
        L.append("")

    dust = by_label["dust_bot"]
    if dust:
        L.append(f"## Dust bots (top {min(top, len(dust))})")
        L.append("")
        L.append(f"Serial wallets whose median snipe is under ${cfg.dust_usd:g}: probes and dust buyers rather than positions.")
        L.append("")
        L.append("| wallet | launches | median buy | total bought | sold within window | median hold |")
        L.append("|---|---:|---:|---:|---:|---:|")
        for s in dust[:top]:
            L.append(f"| {_short(s['wallet'])} | {s['launches']} | {_usd(s['median_snipe_usd'])} | {_usd(s['snipe_usd'])} | {s['sold_in_window']} | {_secs(s['median_hold_in_window_s'])} |")
        L.append("")

    launchers = by_label["serial_launcher"]
    if launchers:
        L.append(f"## Serial launchers (top {min(top, len(launchers))})")
        L.append("")
        L.append("Wallets that were the launch's creator in most of their launches: the token's `developer_address` from CoinGecko token info when known (launchpad tokens), otherwise the buyer in the pool's creation block.")
        L.append("")
        L.append("| wallet | launches | as creator | median buy | sold within window | median hold | round trips |")
        L.append("|---|---:|---:|---:|---:|---:|---:|")
        for s in launchers[:top]:
            L.append(f"| {_short(s['wallet'])} | {s['launches']} | {s['launcher_launches']} | {_usd(s['median_snipe_usd'])} | {s['sold_in_window']} | {_secs(s['median_hold_in_window_s'])} | {s['round_trips']} |")
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

    with_tripper = [o for o in outcomes if o["round_trippers"]]
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
    L.append("- A launch is a token's first pool. Extra pools of an existing token (fee tiers, post-graduation pools) and tapes cut off by the page cap are left out. Same-transaction fee legs on hook pools (a small opposite-side row next to the real trade) are removed before any class is computed.")
    L.append(f"- Every class here is computed by this repo, not supplied by CoinGecko: *snipe* = a buy within {cfg.snipe_s}s of the first trade; *serial* = ≥{cfg.min_launches} sniped launches; *round trip* = a snipe sold within {cfg.roundtrip_max_s}s; *round-tripper* = round trips (or fixed-timer sells) in ≥{cfg.roundtrip_min_rate:.0%} of launches without net profit; *dust bot* = median snipe under ${cfg.dust_usd:g}; *serial launcher* = the developer or creation-block buyer in most launches; *pack* as defined above.")
    L.append(f"- Trades are attributed to the transaction sender. Addresses starting with {', '.join(cfg.bundler_prefixes)} are ERC-4337 bundlers submitting other users' trades and are set aside, so smart-account snipers routed through a bundler are not counted.")
    L.append("- Whole-run counts grow with how long the tracker has run (a wallet needs time to reach 3 launches). Use the last-24h and day-by-day sections to compare periods.")
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
        "pools": len(all_pools),
        "launches": len(pools),
        "labels": dict(labels),
        "packs": len(packs),
        "last24h": {"launches": last["launches"], "with_sniper": last["with_sniper"], "with_tripper": last["with_tripper"], "labels": dict(last["labels"])},
    }
    return text, summary
