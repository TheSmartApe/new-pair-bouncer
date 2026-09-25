"""Offline test of CoinGeckoClient.networks() pagination, via httpx.MockTransport -- no real network call."""
import httpx

from core.client import CoinGeckoClient


def _mock_handler(pages: dict[int, list]):
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        return httpx.Response(200, json={"data": pages.get(page, [])})

    return handler


async def test_networks_paginates_until_an_empty_page():
    pages = {
        1: [{"id": "solana", "type": "network", "attributes": {"name": "Solana"}}],
        2: [{"id": "base", "type": "network", "attributes": {"name": "Base"}}],
        3: [],
    }
    transport = httpx.MockTransport(_mock_handler(pages))
    client = CoinGeckoClient(api_key="test-key", transport=transport)
    try:
        rows = await client.networks()
    finally:
        await client.close()
    assert [r["id"] for r in rows] == ["solana", "base"]
