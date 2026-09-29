"""Wallet enrichment for the top serial snipers: CoinGecko wallet PnL plus their own recent trade tape.

`coingecko_pnl` holds fields returned by GET /onchain/wallets/{address}/pnl as-is. Everything under
`snipes` and `recent` is computed here by FIFO-matching the wallet's own trades
(GET /onchain/networks/{network}/wallets/{address}/trades), restricted for `snipes` to the tokens of
launches this tracker saw the wallet snipe. Wallet trade history reaches back about a week, so
profile wallets while the launches are fresh.
"""
import asyncio
import statistics

from core.client import CoinGeckoClient, CoinGeckoError
from core.wallets import fifo_matches, match_metrics, normalize_trades

NATIVE = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"


def _hold_summary(closed: list[dict]) -> dict:
    m = match_metrics(closed)
    holds = [c["hold_s"] for c in closed]
    m["median_hold_s"] = round(statistics.median(holds), 0) if holds else None
    return m


async def profile_wallet(client: CoinGeckoClient, wallet: str, chains: list[str], sniped_tokens: set[str], max_pages: int = 3) -> dict:
    out: dict = {"wallet": wallet}
    try:
        pnl = await client.get(f"/onchain/wallets/{wallet}/pnl", {"networks": ",".join(chains)}, ttl=300)
        a = (pnl.get("data") or {}).get("attributes") or {}
        stats = a.get("token_stats") or []
        sniped = {t.lower() for t in sniped_tokens if t}

        def realized(rows):
            return round(sum(float(r.get("realized_pnl_usd") or 0) for r in rows), 2)

        # total_realized_pnl_usd includes the wallet's ETH/WETH entry, which can dwarf (and flip the
        # sign of) what it made on tokens. Report the token-only and sniped-token figures next to it.
        tokens_only = [r for r in stats if (r.get("address") or "").lower() != NATIVE and (r.get("symbol") or "").upper() not in ("ETH", "WETH")]
        on_snipes = [r for r in stats if (r.get("address") or "").lower() in sniped]
        out["coingecko_pnl"] = {
            "total_tokens": a.get("total_tokens"),
            "total_realized_pnl_usd": a.get("total_realized_pnl_usd"),
            "total_unrealized_pnl_usd": a.get("total_unrealized_pnl_usd"),
            "realized_excl_eth_usd": realized(tokens_only),
            "realized_on_sniped_tokens_usd": realized(on_snipes),
            "sniped_tokens_in_pnl": len(on_snipes),
            "token_stats_returned": len(stats),
        }
    except CoinGeckoError as exc:
        out["coingecko_pnl"] = {"error": str(exc)[:160]}

    raw: list[dict] = []
    for chain in chains:
        try:
            raw += await client.wallet_trades(chain, wallet, max_pages=max_pages)
        except CoinGeckoError as exc:
            out.setdefault("errors", []).append(f"{chain} trades: {str(exc)[:120]}")
    trades = normalize_trades(raw)
    closed = fifo_matches(trades)
    sniped = {t.lower() for t in sniped_tokens if t}
    on_snipes = [c for c in closed if (c["token"] or "").lower() in sniped]
    out["recent"] = {"trades_seen": len(trades), **_hold_summary(closed)}
    out["snipes"] = {"tokens_matched": len({c["token"] for c in on_snipes}), **_hold_summary(on_snipes)}
    if trades:
        out["recent"]["first_ts"] = trades[0]["ts"]
        out["recent"]["last_ts"] = trades[-1]["ts"]
    return out


async def profile_many(wallets: list[dict], chains: list[str], token_of_pool: dict[str, str]) -> tuple[list[dict], int]:
    """Profiles each wallet stat row; returns (profiles, credits used)."""
    async with CoinGeckoClient() as client:

        async def one(s):
            tokens = {token_of_pool.get(p) for p in s["pools"]}
            return await profile_wallet(client, s["wallet"], chains, {t for t in tokens if t})

        results = await asyncio.gather(*(one(s) for s in wallets))
        return list(results), client.credits_used


async def enrich_launches(launches: list[dict], chain_default: str = "robinhood", client: CoinGeckoClient | None = None) -> tuple[list[dict], int]:
    """token_info for launches (developer address, developer holding %, GT Score, launchpad details).
    developer_address is only populated for launchpad tokens; it is None for plain DEX pools."""
    if client is None:
        async with CoinGeckoClient() as own:
            return await enrich_launches(launches, chain_default, own)
    before = client.credits_used
    if True:

        async def one(o):
            try:
                a = await client.token_info(o["chain"] or chain_default, o["token"])
            except CoinGeckoError as exc:
                return {"chain": o["chain"], "pool": o["pool"], "token": o["token"], "error": str(exc)[:120]}
            return {
                "chain": o["chain"],
                "pool": o["pool"],
                "token": o["token"],
                "developer": (a.get("developer_address") or None),
                "developer_holding_pct": a.get("developer_holding_percentage"),
                "gt_score": a.get("gt_score"),
                "holders": (a.get("holders") or {}).get("count"),
                "launchpad": a.get("launchpad_details"),
            }

        results = await asyncio.gather(*(one(o) for o in launches if o.get("token")))
        return list(results), client.credits_used - before
