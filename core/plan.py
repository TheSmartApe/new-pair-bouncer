"""Detects what an API key's plan unlocks, and a payload helper for locked features."""
from . import config
from .client import CoinGeckoClient, CoinGeckoError

_ANALYST_PLANS = {"analyst", "pro", "pro+", "enterprise", "other"}
_NO_PLAN = {"", "demo", "free"}


async def probe_capabilities(client: CoinGeckoClient) -> dict:
    """Calls GET /key once and returns {paid, analyst, websocket, upgrade_url}; a Demo key gets all False."""
    caps = {"paid": False, "analyst": False, "websocket": False, "upgrade_url": config.PRICING_URL}
    try:
        info = await client.key_usage()
    except CoinGeckoError:
        client.analyst = False
        return caps
    plan = str(info.get("plan") or "").strip().lower()
    if plan not in _NO_PLAN:
        caps["paid"] = True
        caps["analyst"] = plan in _ANALYST_PLANS
        caps["websocket"] = True
    client.analyst = caps["analyst"]
    return caps


def locked(feature: str, upgrade_url: str = config.PRICING_URL) -> dict:
    """Standard payload for a feature the current plan doesn't unlock."""
    return {"locked": True, "feature": feature, "upgrade_url": upgrade_url}
