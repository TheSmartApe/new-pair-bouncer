import pytest

from bot.networks import UnknownNetworkError, ensure_valid, load_networks, validate


class FakeClient:
    """A stand-in for CoinGeckoClient.networks() -- no real network call."""

    def __init__(self, rows):
        self._rows = rows
        self.calls = 0

    async def networks(self):
        self.calls += 1
        return self._rows


def test_validate_passes_known_ids_and_raises_a_clear_error_for_an_unknown_one():
    known = {"solana": "Solana", "base": "Base"}
    validate(["solana", "base"], known)  # no raise
    with pytest.raises(UnknownNetworkError, match="unknown network id 'not-a-chain'"):
        validate(["not-a-chain"], known)


async def test_load_networks_hits_the_client_once_then_serves_from_disk_cache(tmp_path):
    cache = tmp_path / "networks.json"
    client = FakeClient([{"id": "solana", "attributes": {"name": "Solana"}}, {"id": "base", "attributes": {"name": "Base"}}])
    first = await load_networks(client, cache_path=cache)
    assert first == {"solana": "Solana", "base": "Base"}
    assert client.calls == 1

    second = await load_networks(client, cache_path=cache)
    assert second == first
    assert client.calls == 1  # served from the 24h disk cache, no second call


async def test_ensure_valid_raises_for_a_bad_id_in_a_list(tmp_path):
    cache = tmp_path / "networks.json"
    client = FakeClient([{"id": "solana", "attributes": {"name": "Solana"}}])
    with pytest.raises(UnknownNetworkError):
        await ensure_valid(client, ["solana", "not-a-real-chain"], cache_path=cache)
