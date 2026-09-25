"""Validates strategy chain/network ids against the live `/onchain/networks` list.

Strategies can point at any GeckoTerminal network, not just the ones this repo happens to
label in a chain-name dictionary. Rather than hard-coding (and silently going stale on) a list
of "supported" chains, this module asks the API what it actually supports, caches the answer
to disk for 24h, and raises a clear, specific error the moment a strategy names a network id
that doesn't exist -- instead of the scan quietly returning zero candidates from a typo'd chain.
"""
import json
import time
from pathlib import Path

from core import config as core_config
from core.client import CoinGeckoClient

CACHE_PATH = Path("data/cache/networks.json")
CACHE_TTL_S = 24 * 3600


class UnknownNetworkError(ValueError):
    """A strategy or CLI argument named a network id that /onchain/networks doesn't recognize."""


def _read_cache(path: Path = CACHE_PATH) -> dict[str, str] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except (ValueError, OSError):
        return None
    if time.time() - payload.get("fetched_at", 0) > CACHE_TTL_S:
        return None
    networks = payload.get("networks")
    return networks or None


def _write_cache(networks: dict[str, str], path: Path = CACHE_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fetched_at": time.time(), "networks": networks}, indent=2))


async def load_networks(client: CoinGeckoClient, *, force: bool = False, cache_path: Path = CACHE_PATH) -> dict[str, str]:
    """{network_id: display_name} for every network GeckoTerminal supports. Disk-cached 24h so a
    fresh CLI invocation (this bot's every command is a new process) doesn't re-paginate on every call."""
    if not force:
        cached = _read_cache(cache_path)
        if cached:
            return cached
    rows = await client.networks()
    networks = {r.get("id"): ((r.get("attributes") or {}).get("name") or r.get("id")) for r in rows if r.get("id")}
    if networks:
        _write_cache(networks, cache_path)
    return networks


def validate(network_ids: list[str], known: dict[str, str]) -> None:
    """Raises UnknownNetworkError, naming the first id, if any of `network_ids` isn't in `known`."""
    for net in network_ids:
        if net not in known:
            raise UnknownNetworkError(
                f"unknown network id '{net}'. Valid ids come from GET /onchain/networks "
                f"(https://docs.coingecko.com/reference/networks-list) -- for example 'solana', 'eth', 'base', "
                f"'bsc', 'polygon_pos', 'arbitrum'. Check strategies/*.yaml `source.chain`/`source.networks`."
            )


async def ensure_valid(client: CoinGeckoClient, network_ids: list[str], *, cache_path: Path = CACHE_PATH) -> dict[str, str]:
    """Loads the (cached) network list and validates every id in `network_ids`, or raises UnknownNetworkError."""
    known = await load_networks(client, cache_path=cache_path)
    validate(network_ids, known)
    return known


def display_name(network_id: str, known: dict[str, str] | None = None) -> str:
    """A human-readable chain name for `network_id`: the live registry's name, this repo's own
    CHAINS dictionary, or the raw id itself as a last resort."""
    if known and known.get(network_id):
        return known[network_id]
    return core_config.CHAINS.get(network_id, network_id)
