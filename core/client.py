"""Async REST client for the CoinGecko API: caching, retries, credit counting, typed helpers."""
import asyncio
import json
import os
import re
import time
from typing import Any

import httpx

from . import config

_PLAN_HINTS = re.compile(r"pro api subscribers|plan|upgrad|exceed", re.I)


class CoinGeckoError(Exception):
    """A non-2xx response from the CoinGecko API."""

    def __init__(self, status: int, body: str):
        super().__init__(f"CoinGecko HTTP {status}: {body[:200]}")
        self.status = status


class PlanRestrictedError(CoinGeckoError):
    """The API key's plan doesn't cover this endpoint."""


class WrongKeyTypeError(CoinGeckoError):
    """A Pro key hit the demo host, or a Demo key hit the pro host. Check COINGECKO_ENVIRONMENT."""


class CreditBudgetExceeded(CoinGeckoError):
    """The optional local evaluation credit budget was reached before another request."""

    def __init__(self, limit: float):
        super().__init__(429, f"local credit budget reached ({limit:g})")
        self.limit = limit


class NetworkError(CoinGeckoError):
    """A transport-level failure (timeout, connection reset, DNS) rather than an HTTP error response.

    Every call site in this repo already catches CoinGeckoError to degrade gracefully (skip a pool,
    show a locked card, log a reason). Before this wrapped it, a plain network hiccup raised a raw
    httpx exception instead, which none of those `except CoinGeckoError` clauses caught -- so one
    slow response could crash an entire live session (or an autopilot scan) silently, with nothing
    surfaced to the user."""

    def __init__(self, exc: Exception):
        super().__init__(0, f"{type(exc).__name__}: {exc}")


def _error_code(body_text: str) -> int | None:
    """Pulls CoinGecko's app-level status.error_code out of a JSON error body, if present."""
    try:
        body = json.loads(body_text)
    except (ValueError, TypeError):
        return None
    return ((body or {}).get("status") or {}).get("error_code")


class TTLCache:
    """A tiny in-memory cache keyed by path+params, each entry with its own time-to-live."""

    def __init__(self):
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str, ttl: float):
        hit = self._store.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        return None

    def put(self, key: str, value: Any):
        self._store[key] = (time.monotonic(), value)


def _cache_key(path: str, params: dict | None) -> str:
    return path + "?" + "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))


class CoinGeckoClient:
    """Plain async httpx client for every CoinGecko REST endpoint the starter repos use."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self._client = httpx.AsyncClient(
            base_url=base_url or config.BASE_URL,
            headers={config.KEY_HEADER: api_key or config.API_KEY, "accept": "application/json"},
            timeout=30,
            transport=transport,
        )
        self._sem = asyncio.Semaphore(config.CONCURRENCY)
        self.cache = TTLCache()
        self.credits_used = 0
        raw_budget = os.environ.get("COINGECKO_MAX_CREDITS", "").strip()
        try:
            self.max_credits = float(raw_budget) if raw_budget else None
        except ValueError:
            self.max_credits = None
        # Set by plan.probe_capabilities(); None means "unknown, assume full access".
        self.analyst: bool | None = None
        # Human-readable notes on responses this client quietly limited for a lower plan.
        self.degraded: list[str] = []

    async def close(self):
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def get(self, path: str, params: dict | None = None, ttl: float = 0) -> Any:
        """GET with TTL cache, 429 backoff, and plan/key-type error detection. Never logs the URL."""
        key = _cache_key(path, params)
        if ttl:
            hit = self.cache.get(key, ttl)
            if hit is not None:
                return hit
        async with self._sem:
            response = None
            for attempt in range(config.MAX_RETRIES):
                if self.max_credits is not None and self.credits_used >= self.max_credits:
                    raise CreditBudgetExceeded(self.max_credits)
                try:
                    response = await self._client.get(path, params=params)
                except httpx.HTTPError as exc:
                    if attempt < config.MAX_RETRIES - 1:
                        await asyncio.sleep(config.BACKOFF_BASE_S * (attempt + 1))
                        continue
                    raise NetworkError(exc) from exc
                if response.status_code == 429 and attempt < config.MAX_RETRIES - 1:
                    await asyncio.sleep(config.BACKOFF_BASE_S * (attempt + 1))
                    continue
                break
        if response.status_code != 200:
            code = _error_code(response.text)
            if code in (10010, 10011):
                raise WrongKeyTypeError(response.status_code, response.text)
            is_client_error = 400 <= response.status_code < 500
            if code == 10005 or (is_client_error and _PLAN_HINTS.search(response.text)):
                raise PlanRestrictedError(response.status_code, response.text)
            raise CoinGeckoError(response.status_code, response.text)
        self.credits_used += 1
        data = response.json()
        if ttl:
            self.cache.put(key, data)
        return data

    # ---- discovery ----

    async def trending_pools(self, network: str, duration: str = "1h", n: int = 20) -> list[dict]:
        """Trending pools on a network."""
        d = await self.get(
            f"/onchain/networks/{network}/trending_pools",
            {"include": "base_token", "duration": duration, "page": 1},
            ttl=config.TRENDING_TTL_S,
        )
        return d.get("data", [])[:n]

    async def new_pools(self, network: str, n: int = 20) -> list[dict]:
        """Newest pools on a network."""
        d = await self.get(f"/onchain/networks/{network}/new_pools", {"include": "base_token", "page": 1}, ttl=config.TRENDING_TTL_S)
        return d.get("data", [])[:n]

    async def megafilter(self, **filters) -> list[dict]:
        """Pools matching arbitrary /onchain/pools/megafilter filters."""
        d = await self.get("/onchain/pools/megafilter", filters, ttl=config.TRENDING_TTL_S)
        return d.get("data", [])

    async def pools_multi(self, network: str, addresses: list[str]) -> list[dict]:
        """Batch pool lookup, chunked to the API's 30-address limit."""
        out = []
        for i in range(0, len(addresses), 30):
            chunk = addresses[i : i + 30]
            d = await self.get(f"/onchain/networks/{network}/pools/multi/{','.join(chunk)}", {"include": "base_token"}, ttl=config.TRENDING_TTL_S)
            out += d.get("data", [])
        return out

    async def pool(self, network: str, address: str) -> dict:
        """One pool's detail."""
        d = await self.get(f"/onchain/networks/{network}/pools/{address}", {"include": "base_token"}, ttl=30)
        return d.get("data", {})

    async def token_info(self, network: str, address: str) -> dict:
        """A token's GT Score, honeypot flag, authorities, dev holding, and more."""
        d = await self.get(f"/onchain/networks/{network}/tokens/{address}/info", ttl=config.INFO_TTL_S)
        return d.get("data", {}).get("attributes", {})

    # ---- price history ----

    async def pool_ohlcv(self, network: str, address: str, timeframe: str, aggregate: int = 1, limit: int = 1000, before_timestamp: int | None = None) -> list:
        """One page of OHLCV candles for a pool."""
        params = {"aggregate": aggregate, "limit": limit, "currency": "usd", "token": "base"}
        if before_timestamp:
            params["before_timestamp"] = before_timestamp
        d = await self.get(f"/onchain/networks/{network}/pools/{address}/ohlcv/{timeframe}", params)
        return d.get("data", {}).get("attributes", {}).get("ohlcv_list", [])

    async def pool_ohlcv_history(self, network: str, address: str, timeframe: str, aggregate: int = 1, pages: int = 3) -> list:
        """Walks before_timestamp backwards to build a longer candle history."""
        out: list = []
        before = None
        for _ in range(pages):
            batch = await self.pool_ohlcv(network, address, timeframe, aggregate, before_timestamp=before)
            if not batch:
                break
            out += batch
            before = min(row[0] for row in batch)
        return out

    # ---- trades ----

    async def pool_trades(self, network: str, address: str, trading_period: str | None = None, max_pages: int = 1) -> list[dict]:
        """Cursor-paginated trades for a pool; degrades quietly to the last 300/24h below Analyst+."""
        wants_paging = bool(trading_period) or max_pages > 1
        if wants_paging and self.analyst is False:
            self.degraded.append(f"pool_trades {network}/{address}: trading_period+paging need Analyst+, showing last 300 trades from 24h")
            return await self._paginate(f"/onchain/networks/{network}/pools/{address}/trades", {"token": "base"}, 1)
        params: dict = {"token": "base"}
        if trading_period:
            params["trading_period"] = trading_period
        return await self._paginate(f"/onchain/networks/{network}/pools/{address}/trades", params, max_pages)

    async def pool_trades_range(self, network: str, address: str, from_ts: int, to_ts: int, max_pages: int = 5) -> list[dict]:
        """Trades for a pool inside an absolute [from_ts, to_ts] window (30-day cap), cursor-paginated."""
        params = {"from": from_ts, "to": to_ts}
        return await self._paginate(f"/onchain/networks/{network}/pools/{address}/trades/range", params, max_pages)

    async def _paginate(self, path: str, params: dict, max_pages: int) -> list[dict]:
        """Follows meta.next_cursor up to max_pages, flattening each row's attributes."""
        out: list[dict] = []
        cursor = None
        for _ in range(max_pages):
            page_params = dict(params)
            if cursor:
                page_params["cursor"] = cursor
            d = await self.get(path, page_params)
            rows = d.get("data", [])
            out += [r.get("attributes", r) for r in rows]
            cursor = (d.get("meta") or {}).get("next_cursor")
            if not cursor or not rows:
                break
        return out

    # ---- smart money ----

    async def top_traders(self, network: str, token: str, n: int = 25, sort: str = "realized_pnl_usd_desc") -> list[dict]:
        """Top traders of a token, ranked by `sort`."""
        d = await self.get(f"/onchain/networks/{network}/tokens/{token}/top_traders", {"traders": n, "sort": sort, "include_address_label": "true"}, ttl=120)
        return d.get("data", {}).get("attributes", {}).get("traders", [])

    async def top_holders(self, network: str, token: str, n: int = 20) -> list[dict]:
        """Top holders of a token."""
        d = await self.get(f"/onchain/networks/{network}/tokens/{token}/top_holders", {"holders": n}, ttl=120)
        return d.get("data", {}).get("attributes", {}).get("holders", [])

    # ---- wallets ----

    async def wallet_pnl(self, address: str, networks: list[str]) -> dict:
        """Realized/unrealized PnL across the networks that support the pnl endpoint."""
        nets = [n for n in networks if config.wallet_caps(n).get("pnl")]
        d = await self.get(f"/onchain/wallets/{address}/pnl", {"networks": ",".join(nets)}, ttl=300)
        return d.get("data", {}).get("attributes", {})

    async def wallet_trades(self, network: str, address: str, max_pages: int = config.DEFAULT_MAX_PAGES) -> list[dict]:
        """Cursor-paginated swap history for a wallet on one network."""
        return await self._paginate(f"/onchain/networks/{network}/wallets/{address}/trades", {}, max_pages)

    async def wallet_balances(self, address: str, networks: list[str]) -> dict:
        """Current token balances across the EVM networks that support the balances endpoint."""
        nets = [n for n in networks if config.wallet_caps(n).get("balances")]
        d = await self.get(f"/onchain/wallets/{address}/balances", {"networks": ",".join(nets)})
        return d.get("data", {}).get("attributes", {})

    async def wallet_transfers(self, network: str, address: str, max_pages: int = config.DEFAULT_MAX_PAGES) -> list[dict]:
        """Cursor-paginated raw transfers for a wallet on one network (30-day window)."""
        return await self._paginate(f"/onchain/networks/{network}/wallets/{address}/transfers", {}, max_pages)

    # ---- account + market ----

    async def key_usage(self) -> dict:
        """Current plan and rate-limit info for this key."""
        return await self.get("/key")

    async def coins_markets(self, vs_currency: str = "usd", n: int = 100, ids: list[str] | None = None) -> list[dict]:
        """CEX market data, for sanity checks and price context."""
        params = {"vs_currency": vs_currency, "per_page": min(n, 250), "page": 1}
        if ids:
            params["ids"] = ",".join(ids)
        return await self.get("/coins/markets", params, ttl=60)

    async def simple_price(self, ids: list[str], vs_currencies: str = "usd") -> dict:
        """The cheapest price lookup, by coin id."""
        return await self.get("/simple/price", {"ids": ",".join(ids), "vs_currencies": vs_currencies}, ttl=30)
