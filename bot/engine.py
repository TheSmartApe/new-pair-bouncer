"""Turns passing candidates into paper entries, and watches open positions for take-profit / stop-loss / max-hold exits."""
import time
from dataclasses import dataclass, field

from core.client import CoinGeckoClient
from core.paper import Portfolio

from .strategy import Evaluation, Strategy


@dataclass
class PositionMeta:
    """The extra context Portfolio doesn't track: which pool a symbol key actually is."""

    chain: str
    address: str
    symbol: str
    strategy: str
    pair: str = ""  # the pool's pair name, e.g. "COIN / SOL", for dashboard display


@dataclass
class Engine:
    """Wraps a Portfolio with pool metadata, entry sizing, and exit-rule evaluation."""

    portfolio: Portfolio
    strategy: Strategy
    meta: dict[str, PositionMeta] = field(default_factory=dict)
    ws_prices: dict[str, float] | None = None  # optional fast path, keyed "{chain}_{address}" (core.ws pool-address format)

    def _key(self, chain: str, address: str) -> str:
        return f"{chain}:{address}"

    def open_pool_keys(self) -> list[str]:
        """Every open position's pool address in the "network_pool" format core.ws expects."""
        return [f"{m.chain}_{m.address}" for m in self.meta.values()]

    def try_enter(self, ev: Evaluation) -> dict | None:
        """Buys `entry.budget_usd` of a passing candidate at its scan-time price. Returns a decision record, or None if it didn't fill."""
        c = ev.candidate
        price = c.get("price_usd")
        if not price or price <= 0:
            return {"action": "entry_skipped", "symbol": c.get("symbol"), "reason": "no usable price at scan time"}
        key = self._key(c["chain"], c["address"])
        budget = self.strategy.entry.get("budget_usd", 50)
        ts = time.time()
        filled = self.portfolio.buy(key, ts, price, usd=budget)
        if not filled:
            return {"action": "entry_skipped", "symbol": c.get("symbol"), "reason": "on cooldown, no cash, or position-size cap reached"}
        self.meta[key] = PositionMeta(chain=c["chain"], address=c["address"], symbol=c.get("symbol", key), strategy=self.strategy.name, pair=c.get("name", ""))
        return {
            "action": "entry",
            "symbol": c.get("symbol"),
            "chain": c["chain"],
            "address": c["address"],
            "price_usd": price,
            "budget_usd": budget,
            "checks": [{"name": ch.name, "passed": ch.passed, "reason": ch.reason} for ch in ev.checks],
        }

    async def current_price(self, client: CoinGeckoClient, chain: str, address: str) -> float | None:
        """The WebSocket feed's price if one has arrived (paid plans), else the cached pool detail call."""
        if self.ws_prices:
            ws_price = self.ws_prices.get(f"{chain}_{address}")
            if ws_price:
                return ws_price
        try:
            pool = await client.pool(chain, address)
            attrs = pool.get("attributes", pool) if isinstance(pool, dict) else {}
            price = attrs.get("base_token_price_usd")
            return float(price) if price else None
        except Exception:
            return None

    async def monitor_open_positions(self, client: CoinGeckoClient) -> list[dict]:
        """Checks every open position's exit rules against its current price. Returns one decision dict per closed trade."""
        decisions = []
        now = time.time()
        exits = self.strategy.exits
        for key in list(self.portfolio.positions.keys()):
            pos = self.portfolio.positions.get(key)
            meta = self.meta.get(key)
            if not pos or not meta:
                continue
            price = await self.current_price(client, meta.chain, meta.address)
            if price is None:
                continue
            entry_price = pos.cost_usd / pos.qty if pos.qty else None
            if not entry_price:
                continue
            change_pct = (price - entry_price) / entry_price * 100
            hold_min = (now - (pos.opened_ts or now)) / 60
            reason = None
            if exits.get("take_profit_pct") is not None and change_pct >= exits["take_profit_pct"]:
                reason = f"take-profit: +{change_pct:.1f}% >= {exits['take_profit_pct']}%"
            elif exits.get("stop_loss_pct") is not None and change_pct <= -exits["stop_loss_pct"]:
                reason = f"stop-loss: {change_pct:.1f}% <= -{exits['stop_loss_pct']}%"
            elif exits.get("max_hold_minutes") is not None and hold_min >= exits["max_hold_minutes"]:
                reason = f"max hold: {hold_min:.0f}min >= {exits['max_hold_minutes']}min"
            if reason and self.portfolio.sell(key, now, price):
                decisions.append(
                    {
                        "action": "exit",
                        "symbol": meta.symbol,
                        "chain": meta.chain,
                        "address": meta.address,
                        "price_usd": price,
                        "change_pct": round(change_pct, 2),
                        "hold_minutes": round(hold_min, 1),
                        "reason": reason,
                    }
                )
                self.meta.pop(key, None)
        prices = {}
        for key, meta in self.meta.items():
            p = await self.current_price(client, meta.chain, meta.address)
            if p:
                prices[key] = p
        self.portfolio.snapshot(now, prices)
        return decisions

    def open_positions_view(self, last_prices: dict[str, float]) -> list[dict]:
        """A dashboard-ready view of every open position: symbol, entry, current, unrealized P&L, hold time."""
        now = time.time()
        rows = []
        for key, pos in self.portfolio.positions.items():
            meta = self.meta.get(key)
            entry_price = pos.cost_usd / pos.qty if pos.qty else 0
            price = last_prices.get(key, entry_price)
            change_pct = (price - entry_price) / entry_price * 100 if entry_price else 0
            rows.append(
                {
                    "symbol": meta.symbol if meta else key,
                    "chain": meta.chain if meta else "",
                    "pair": (meta.pair if meta else "") or (meta.symbol if meta else key),
                    "entry_price": entry_price,
                    "price": price,
                    "change_pct": change_pct,
                    "hold_minutes": (now - (pos.opened_ts or now)) / 60,
                    "cost_usd": pos.cost_usd,
                }
            )
        return rows
