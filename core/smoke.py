"""Tiny live smoke test: `python -m core.smoke`. Never prints the key. Uses a handful of credits."""
import asyncio

from . import config, links
from .client import CoinGeckoClient, PlanRestrictedError
from .plan import probe_capabilities


async def main():
    client = CoinGeckoClient()
    try:
        caps = await probe_capabilities(client)
        print(f"[plan] environment={config.ENVIRONMENT} paid={caps['paid']} analyst={caps['analyst']} websocket={caps['websocket']}")

        pools = await client.trending_pools("solana", "1h", 5)
        print(f"[trending_pools] solana 1h -> {len(pools)} pools")

        try:
            if pools:
                address = pools[0]["attributes"]["address"]
                candles = await client.pool_ohlcv("solana", address, "minute", limit=10)
                print(f"[pool_ohlcv] solana -> {len(candles)} candles")
        except PlanRestrictedError:
            print("[pool_ohlcv] locked on this plan")

        try:
            if caps["analyst"] and pools:
                token_id = pools[0]["relationships"]["base_token"]["data"]["id"]
                address = token_id.split("_", 1)[1]
                traders = await client.top_traders("solana", address, n=2)
                if traders:
                    pnl = await client.wallet_pnl(traders[0]["address"], ["solana"])
                    print(f"[wallet_pnl] tokens_traded={pnl.get('total_tokens')}")
                else:
                    print("[wallet_pnl] skipped: no top traders returned")
            else:
                print("[wallet_pnl] skipped: needs Analyst+")
        except PlanRestrictedError:
            print("[wallet_pnl] locked on this plan")

        sample = "See the [CoinGecko API](https://www.coingecko.com/en/api) docs."
        print(f"[set_link dry run] {links.rewrite_text(sample, '@TestCreator', source='github')}")

        print(f"[credits] used={client.credits_used}")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
