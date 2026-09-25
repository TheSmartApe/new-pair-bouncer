"""Offline tests for multi-network candidate fetching in bot/scan.py -- no real network calls."""
from bot.scan import fetch_candidates
from bot.strategy import Strategy


def _pool_row(network: str, symbol: str = "COIN"):
    return {
        "attributes": {
            "address": f"{network}-pool",
            "name": f"{symbol} / SOL",
            "reserve_in_usd": "10000",
            "volume_usd": {"h24": "5000"},
            "transactions": {"h24": {"buys": 5, "sells": 5}},
        },
        "relationships": {"network": {"data": {"id": network}}},
    }


class FakeClient:
    def __init__(self):
        self.trending_calls = []
        self.megafilter_calls = []

    async def trending_pools(self, network, duration="1h", n=20):
        self.trending_calls.append((network, duration, n))
        return [_pool_row(network)]

    async def new_pools(self, network, n=20):
        self.trending_calls.append((network, "new_pools", n))
        return [_pool_row(network)]

    async def megafilter(self, **filters):
        self.megafilter_calls.append(filters)
        return [_pool_row("solana"), _pool_row("base")]


async def test_fetch_candidates_merges_trending_across_a_network_list():
    strategy = Strategy({"name": "t", "source": {"type": "trending", "networks": ["solana", "base"], "n": 10}})
    client = FakeClient()
    candidates, note, locked = await fetch_candidates(client, strategy, {"analyst": True})
    assert {c["chain"] for c in candidates} == {"solana", "base"}
    assert "solana" in note and "base" in note
    assert not locked
    # n=10 split across 2 networks -> 5 requested per network
    assert all(n == 5 for (_, _, n) in client.trending_calls)


async def test_fetch_candidates_passes_networks_list_to_megafilter_in_one_call():
    strategy = Strategy({"name": "m", "source": {"type": "megafilter", "networks": ["solana", "base"], "n": 10}})
    client = FakeClient()
    candidates, note, locked = await fetch_candidates(client, strategy, {"analyst": True})
    assert len(client.megafilter_calls) == 1
    assert client.megafilter_calls[0]["networks"] == "solana,base"
    assert {c["chain"] for c in candidates} == {"solana", "base"}
    assert not locked


async def test_megafilter_falls_back_to_trending_per_network_without_analyst():
    strategy = Strategy({"name": "m", "source": {"type": "megafilter", "networks": ["solana", "base"], "n": 10}})
    client = FakeClient()
    candidates, note, locked = await fetch_candidates(client, strategy, {"analyst": False})
    assert not client.megafilter_calls
    assert {net for net, _, _ in client.trending_calls} == {"solana", "base"}
    assert locked and "Analyst plan" in locked[0]
