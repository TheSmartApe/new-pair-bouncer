"""Flattens raw CoinGecko onchain API rows into the plain dicts filters.py and strategy.py work with."""
import time
from datetime import datetime


def _f(x, default=None):
    """float(x), or default if x is missing or not a number."""
    if x is None or x == "":
        return default
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _get(d: dict, *path, default=None):
    """Walks nested dict keys, returning `default` the moment any key is missing."""
    cur = d
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur if cur is not None else default


def _epoch(value) -> float | None:
    """A unix timestamp or an ISO8601 string, normalized to unix seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def normalize_pool(raw: dict, chain: str) -> dict:
    """One /onchain/networks/{chain}/{trending_pools|new_pools|megafilter} row -> a flat candidate dict."""
    attrs = raw.get("attributes", raw) or {}
    rel = raw.get("relationships") or {}
    token_id = _get(rel, "base_token", "data", "id")
    address = attrs.get("address") or (token_id.split("_", 1)[1] if token_id and "_" in token_id else None)
    name = attrs.get("name") or ""
    symbol = name.split("/")[0].strip() if "/" in name else name
    txns_h24 = _get(attrs, "transactions", "h24", default={}) or {}
    return {
        "id": raw.get("id"),
        "chain": chain,
        "address": address,
        "name": name,
        "symbol": symbol or address,
        "pool_created_at": _epoch(attrs.get("pool_created_at")),
        "liquidity_usd": _f(attrs.get("reserve_in_usd")),
        "volume_24h_usd": _f(_get(attrs, "volume_usd", "h24")),
        "price_usd": _f(attrs.get("base_token_price_usd")),
        "price_change_pct_24h": _f(_get(attrs, "price_change_percentage", "h24")),
        "price_change_pct_1h": _f(_get(attrs, "price_change_percentage", "h1")),
        "buys_24h": _get(txns_h24, "buys", default=0) or 0,
        "sells_24h": _get(txns_h24, "sells", default=0) or 0,
        "fdv_usd": _f(attrs.get("fdv_usd")),
    }


def normalize_token_info(attrs: dict | None) -> dict:
    """A token_info response -> a flat dict. Missing fields come back as None (filters treat None as "unknown")."""
    attrs = attrs or {}
    gt_score = _f(attrs.get("gt_score"))
    is_honeypot = attrs.get("is_honeypot")
    if is_honeypot is None:
        is_honeypot = _get(attrs, "additional_data", "is_honeypot")
    holders = _get(attrs, "holders", default={}) or {}
    top10_pct = _f(holders.get("top_10_holder_percentage") or attrs.get("top_10_holder_percentage"))
    dev_pct = _f(attrs.get("holder_developer_percentage") or attrs.get("developer_holding_percentage"))
    return {
        "gt_score": gt_score,
        "is_honeypot": bool(is_honeypot) if isinstance(is_honeypot, bool) else is_honeypot,
        "top10_holder_pct": top10_pct,
        "dev_holding_pct": dev_pct,
        "mint_authority_revoked": attrs.get("mint_authority") in (None, "", "revoked") or attrs.get("mint_authority_revoked") is True,
        "freeze_authority_revoked": attrs.get("freeze_authority") in (None, "", "revoked") or attrs.get("freeze_authority_revoked") is True,
        "holder_count": attrs.get("holder_count") or _get(holders, "count"),
    }


def top_trader_realized_pnl(traders: list[dict]) -> float | None:
    """Best-of realized PnL among a token's top traders, in USD; None if the list is empty or unlabeled."""
    values = [_f(t.get("realized_pnl_usd") or _get(t, "attributes", "realized_pnl_usd")) for t in (traders or [])]
    values = [v for v in values if v is not None]
    return max(values) if values else None


def now_ts() -> float:
    return time.time()
