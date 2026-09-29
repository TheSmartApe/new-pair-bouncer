"""Money: what serial wallets put into the launches they sniped, and what they took out.

For every serial wallet, the CoinGecko wallet PnL endpoint (GET /onchain/wallets/{address}/pnl)
gives per-token total_buy_usd, total_sell_usd, realized and unrealized PnL. Its token_stats come
100 per page, sorted by realized PnL (biggest winners first), so the pages are walked until every
sniped token is found. Wallets with thousands of tokens fall back to FIFO over their own trade history
(GET /onchain/networks/{network}/wallets/{address}/trades).

The headline figures are CASH: total sold minus total bought on the sniped tokens. Unrealized PnL is
kept for reference only: it marks leftover bags of tiny tokens at a last price that usually can't be
sold into, and it can turn a wallet that lost cash into an apparent big winner.
"""
import asyncio
import json
import statistics
import time
from collections import defaultdict

from core.client import CoinGeckoClient, CoinGeckoError, CreditBudgetExceeded
from core.wallets import fifo_matches, normalize_trades

from . import analyze
from .config import SniperConfig

MONEY_CLASSES = ("serial_sniper", "round_tripper", "dust_bot")


async def _via_stats(client: CoinGeckoClient, wallet: str, chain: str, want: set[str], max_pages: int) -> tuple[dict, int]:
    found: dict[str, dict] = {}
    page, total = 1, 0
    while True:
        d = await client.get(f"/onchain/wallets/{wallet}/pnl", {"networks": chain, "page": page})
        a = (d.get("data") or {}).get("attributes") or {}
        total = a.get("total_tokens") or 0
        rows = a.get("token_stats") or []
        for r in rows:
            addr = (r.get("address") or "").lower()
            if addr in want:
                found[addr] = r
        if len(found) == len(want) or not rows or page * 100 >= total or page >= max_pages:
            return found, total
        page += 1


async def _via_fifo(client: CoinGeckoClient, wallet: str, chain: str, want: set[str], since_ts: float) -> dict:
    raw: list[dict] = []
    cursor = None
    for _ in range(25):
        d = await client.get(f"/onchain/networks/{chain}/wallets/{wallet}/trades", {"cursor": cursor} if cursor else {})
        page = d.get("data", [])
        raw += [r.get("attributes", r) for r in page]
        cursor = (d.get("meta") or {}).get("next_cursor")
        oldest = min((analyze.iso_ts(r.get("block_timestamp")) or 1e18) for r in raw) if raw else 1e18
        if not cursor or not page or oldest < since_ts - 600:
            break
    trades = [t for t in normalize_trades(raw) if (t["token"] or "").lower() in want]
    per: dict[str, dict] = defaultdict(lambda: {"symbol": None, "realized": 0.0, "unrealized": None, "buy": 0.0, "sell": 0.0, "fifo": True})
    for t in trades:
        per[t["token"].lower()]["buy" if t["kind"] == "buy" else "sell"] += t["usd"]
    for c in fifo_matches(trades):
        per[c["token"].lower()]["realized"] += c["pnl_usd"]
    return dict(per)


async def fetch_money(stats: dict[str, dict], positions: dict, token_of: dict, cfg: SniperConfig, max_stats_pages: int = 15) -> tuple[dict, int]:
    """wallet -> {total_tokens, sniped, tokens: {address: {symbol, realized, unrealized, buy, sell}}}."""
    sniped: dict[str, set[str]] = defaultdict(set)
    chain_of: dict[str, str] = {}
    for (chain, pool, wallet) in positions:
        if stats.get(wallet, {}).get("label") in MONEY_CLASSES and token_of.get((chain, pool)):
            sniped[wallet].add(token_of[(chain, pool)].lower())
            chain_of[wallet] = chain
    out: dict[str, dict] = {}
    async with CoinGeckoClient() as client:

        async def one(wallet: str):
            want, chain = sniped[wallet], chain_of[wallet]
            try:
                found, total = await _via_stats(client, wallet, chain, want, max_stats_pages)
                rec = {"total_tokens": total, "sniped": len(want), "tokens": {}}
                for addr, r in found.items():
                    rec["tokens"][addr] = {
                        "symbol": r.get("symbol"),
                        "realized": float(r.get("realized_pnl_usd") or 0),
                        "unrealized": float(r.get("unrealized_pnl_usd") or 0),
                        "buy": float(r.get("total_buy_usd") or 0),
                        "sell": float(r.get("total_sell_usd") or 0),
                    }
                missing = want - set(found)
                if missing:
                    for addr, t in (await _via_fifo(client, wallet, chain, missing, stats[wallet]["first_ts"])).items():
                        rec["tokens"][addr] = t
                out[wallet] = rec
            except CreditBudgetExceeded:
                raise
            except CoinGeckoError as exc:
                out[wallet] = {"error": str(exc)[:160]}

        await asyncio.gather(*(one(w) for w in sniped))
        return out, client.credits_used


def summarize(money: dict[str, dict], stats: dict[str, dict]) -> dict:
    """Headline cash figures per class, the winners-vs-losers comparison, and the leaderboard."""

    def med(xs):
        xs = [x for x in xs if x is not None]
        return round(statistics.median(xs), 2) if xs else None

    out: dict = {"classes": {}, "leaderboard": [], "best_position": None}
    best = None
    for label in MONEY_CLASSES:
        ws = {w: r for w, r in money.items() if "error" not in r and stats.get(w, {}).get("label") == label and r["tokens"]}
        if not ws:
            continue
        cash = {w: sum(t["sell"] - t["buy"] for t in r["tokens"].values()) for w, r in ws.items()}
        put_in = sum(t["buy"] for r in ws.values() for t in r["tokens"].values())
        took_out = sum(t["sell"] for r in ws.values() for t in r["tokens"].values())
        positions = [t for r in ws.values() for t in r["tokens"].values() if t["buy"] > 0]
        winners = [w for w, v in cash.items() if v > 0]
        losers = [w for w, v in cash.items() if v <= 0]
        top = sorted(cash.items(), key=lambda kv: -kv[1])
        win_sum = sum(cash[w] for w in winners)
        out["classes"][label] = {
            "wallets": len(ws),
            "put_in_usd": round(put_in, 2),
            "took_out_usd": round(took_out, 2),
            "net_cash_usd": round(took_out - put_in, 2),
            "cash_winners": len(winners),
            "winners_usd": round(win_sum, 2),
            "losers_usd": round(sum(cash[w] for w in losers), 2),
            "median_wallet_cash_usd": med(cash.values()),
            "top10_share_of_winnings": round(sum(v for _, v in top[:10] if v > 0) / win_sum, 3) if win_sum else None,
            "positions": len(positions),
            "positions_cash_positive_pct": round(100 * sum(1 for t in positions if t["sell"] > t["buy"]) / len(positions), 1) if positions else None,
            "positions_2x_pct": round(100 * sum(1 for t in positions if t["sell"] >= 2 * t["buy"]) / len(positions), 1) if positions else None,
            "median_position_cash_usd": med(t["sell"] - t["buy"] for t in positions),
            "median_position_buy_usd": med(t["buy"] for t in positions),
            "unrealized_usd_reference_only": round(sum((t["unrealized"] or 0) for r in ws.values() for t in r["tokens"].values()), 2),
            "winners_profile": {
                "median_entry_s": med(stats[w]["median_sec_offset"] for w in winners),
                "median_entry_blocks": med(stats[w]["median_block_offset"] for w in winners),
                "median_buy_usd": med(stats[w]["median_snipe_usd"] for w in winners),
                "median_hold_s": med(stats[w]["median_hold_in_window_s"] for w in winners),
                "sold_within_window_rate": med(stats[w]["sold_in_window"] / stats[w]["launches"] for w in winners),
            },
            "losers_profile": {
                "median_entry_s": med(stats[w]["median_sec_offset"] for w in losers),
                "median_entry_blocks": med(stats[w]["median_block_offset"] for w in losers),
                "median_buy_usd": med(stats[w]["median_snipe_usd"] for w in losers),
                "median_hold_s": med(stats[w]["median_hold_in_window_s"] for w in losers),
                "sold_within_window_rate": med(stats[w]["sold_in_window"] / stats[w]["launches"] for w in losers),
            },
        }
        if label == "serial_sniper":
            for w, v in top[:25]:
                s = stats[w]
                out["leaderboard"].append({
                    "wallet": w, "cash_usd": round(v, 2), "launches": s["launches"], "entry_s": s["median_sec_offset"],
                    "entry_blocks": s["median_block_offset"], "median_buy_usd": s["median_snipe_usd"], "median_hold_s": s["median_hold_in_window_s"],
                })
            for w, r in ws.items():
                for addr, t in r["tokens"].items():
                    if best is None or t["sell"] - t["buy"] > best["cash_usd"]:
                        best = {"wallet": w, "token": addr, "symbol": t["symbol"], "buy_usd": round(t["buy"], 2), "sell_usd": round(t["sell"], 2), "cash_usd": round(t["sell"] - t["buy"], 2)}
    out["best_position"] = best
    return out


def run(store, cfg: SniperConfig) -> dict:
    """Fetch money for every serial wallet over the analysis window, store it, return the summary."""
    since = time.time() - cfg.analysis_days * 86400 if cfg.analysis_days else None
    data = store.load_for_analysis(cfg.snipe_s, since=since)
    pools, trades = analyze.prepare(data["pools"], data["trades"], cfg, since=data.get("since") or None)
    stats = analyze.wallet_stats(trades, pools, cfg, info=data["info"])
    positions = analyze.launch_positions(trades, cfg, pools, data["info"])
    token_of = {(p["chain"], p["pool"]): p["token"] for p in pools}
    money, credits = asyncio.run(fetch_money(stats, positions, token_of, cfg))
    summary = summarize(money, stats)
    summary["computed_ts"] = time.time()
    summary["launches"] = len(pools)
    summary["hours"] = round(analyze.observation_hours(pools), 2)
    summary["credits"] = credits
    store.save_money(summary)
    store.commit()
    return summary


def fmt(summary: dict) -> str:
    """Plain-text version for the terminal."""
    lines = [f"{summary.get('launches', 0)} launches over {summary.get('hours', 0)}h, {summary.get('credits', 0)} credits"]
    for label, c in summary["classes"].items():
        lines.append(
            f"{label}: {c['wallets']} wallets put in ${c['put_in_usd']:,.0f}, took out ${c['took_out_usd']:,.0f} (net ${c['net_cash_usd']:,.0f}). "
            f"{c['cash_winners']} made money: +${c['winners_usd']:,.0f}; the rest lost ${-c['losers_usd']:,.0f}. "
            f"{c['positions_cash_positive_pct']}% of {c['positions']} positions made money, {c['positions_2x_pct']}% doubled, median position ${c['median_position_cash_usd']} on ${c['median_position_buy_usd']}."
        )
        wp, lp = c["winners_profile"], c["losers_profile"]
        lines.append(f"  winners: entry +{wp['median_entry_s']}s/{wp['median_entry_blocks']} blk, buy ${wp['median_buy_usd']}, hold {wp['median_hold_s']}s, sold within 2 min {wp['sold_within_window_rate']}")
        lines.append(f"  losers:  entry +{lp['median_entry_s']}s/{lp['median_entry_blocks']} blk, buy ${lp['median_buy_usd']}, hold {lp['median_hold_s']}s, sold within 2 min {lp['sold_within_window_rate']}")
    b = summary.get("best_position")
    if b:
        lines.append(f"best position: {b['wallet'][:10]} ${b['buy_usd']:,.0f} into {b['symbol'] or b['token'][:10]}, ${b['sell_usd']:,.0f} out")
    return "\n".join(lines)


def to_json(summary: dict) -> str:
    return json.dumps(summary)
