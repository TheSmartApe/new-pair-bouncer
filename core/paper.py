"""Pure, synchronous paper-trading engine: fills, positions, equity curve, closed-trade metrics."""
import statistics
from dataclasses import dataclass


@dataclass
class Position:
    """One open paper position in a symbol."""

    qty: float = 0.0
    cost_usd: float = 0.0
    opened_ts: float | None = None


@dataclass
class ClosedTrade:
    """One completed round-trip, logged when a sell closes some or all of a position."""

    symbol: str
    qty: float
    buy_usd: float
    sell_usd: float
    pnl_usd: float
    opened_ts: float
    closed_ts: float


class Portfolio:
    """Paper positions funded by `cash`, with slippage/fee/position-size/cooldown assumptions applied at fill time."""

    def __init__(self, cash: float, slippage_bps: float = 0, fee_bps: float = 0, max_position_pct: float = 1.0, cooldown_s: float = 0):
        self.cash = cash
        self.starting_cash = cash
        self.slippage_bps = slippage_bps
        self.fee_bps = fee_bps
        self.max_position_pct = max_position_pct
        self.cooldown_s = cooldown_s
        self.positions: dict[str, Position] = {}
        self.closed: list[ClosedTrade] = []
        self.equity_curve: list[tuple[float, float]] = []
        self._last_trade_ts: dict[str, float] = {}

    def _fill_price(self, observed_price: float, side: str) -> float:
        """Observed price moved against you by slippage_bps."""
        slip = observed_price * (self.slippage_bps / 10_000)
        return observed_price + slip if side == "buy" else observed_price - slip

    def _on_cooldown(self, symbol: str, ts: float) -> bool:
        last = self._last_trade_ts.get(symbol)
        return last is not None and ts - last < self.cooldown_s

    def equity(self, prices: dict[str, float]) -> float:
        """Cash plus every open position marked at `prices` (falls back to cost basis if a price is missing)."""
        value = self.cash
        for symbol, pos in self.positions.items():
            value += pos.qty * prices.get(symbol, pos.cost_usd / pos.qty if pos.qty else 0.0)
        return value

    def snapshot(self, ts: float, prices: dict[str, float]):
        """Records one equity-curve point."""
        self.equity_curve.append((ts, self.equity(prices)))

    def buy(self, symbol: str, ts: float, price: float, usd: float | None = None, fraction: float | None = None) -> bool:
        """Buys `usd` (or `fraction` of cash) of `symbol` at `price` plus slippage and fees. Returns whether it filled."""
        if self._on_cooldown(symbol, ts):
            return False
        total = self.cash + sum(p.cost_usd for p in self.positions.values())
        spend = usd if usd is not None else (fraction or 0) * self.cash
        existing = self.positions.get(symbol)
        room = max(self.max_position_pct * total - (existing.cost_usd if existing else 0), 0)
        spend = max(min(spend, self.cash, room), 0)
        if spend <= 0:
            return False
        fill = self._fill_price(price, "buy")
        fee = spend * (self.fee_bps / 10_000)
        qty = (spend - fee) / fill if fill else 0.0
        if qty <= 0:
            return False
        self.cash -= spend
        pos = self.positions.setdefault(symbol, Position())
        pos.qty += qty
        pos.cost_usd += spend
        if pos.opened_ts is None:
            pos.opened_ts = ts
        self._last_trade_ts[symbol] = ts
        return True

    def sell(self, symbol: str, ts: float, price: float, usd: float | None = None, fraction: float | None = None) -> bool:
        """Sells `usd` (or `fraction` of the position) of `symbol` and logs a closed trade with realized PnL."""
        if self._on_cooldown(symbol, ts):
            return False
        pos = self.positions.get(symbol)
        if not pos or pos.qty <= 0:
            return False
        fill = self._fill_price(price, "sell")
        qty = min(usd / fill, pos.qty) if usd is not None and fill else pos.qty * (fraction if fraction is not None else 1.0)
        qty = max(min(qty, pos.qty), 0)
        if qty <= 0:
            return False
        proceeds = qty * fill
        proceeds -= proceeds * (self.fee_bps / 10_000)
        cost_share = pos.cost_usd * (qty / pos.qty)
        self.cash += proceeds
        pos.qty -= qty
        pos.cost_usd -= cost_share
        self.closed.append(
            ClosedTrade(symbol=symbol, qty=qty, buy_usd=cost_share, sell_usd=proceeds, pnl_usd=proceeds - cost_share, opened_ts=pos.opened_ts or ts, closed_ts=ts)
        )
        self._last_trade_ts[symbol] = ts
        if pos.qty <= 1e-12:
            del self.positions[symbol]
        return True

    def metrics(self, prices: dict[str, float] | None = None) -> dict:
        """trades, win_rate, pnl_usd, pnl_pct, max_drawdown_pct, exposure, avg_hold_s."""
        prices = prices or {}
        equity_now = self.equity(prices)
        curve = [e for _, e in self.equity_curve] or [self.starting_cash, equity_now]
        peak, max_dd = curve[0], 0.0
        for e in curve:
            peak = max(peak, e)
            if peak > 0:
                max_dd = max(max_dd, (peak - e) / peak)
        wins = sum(1 for t in self.closed if t.pnl_usd > 0)
        return {
            "trades": len(self.closed),
            "win_rate": round(wins / len(self.closed), 3) if self.closed else None,
            "pnl_usd": round(equity_now - self.starting_cash, 2),
            "pnl_pct": round((equity_now - self.starting_cash) / self.starting_cash * 100, 2) if self.starting_cash else None,
            "max_drawdown_pct": round(max_dd * 100, 2),
            "exposure_usd": round(sum(p.cost_usd for p in self.positions.values()), 2),
            "avg_hold_s": round(statistics.mean(t.closed_ts - t.opened_ts for t in self.closed), 0) if self.closed else None,
            "closed_pnl_usd": round(sum(t.pnl_usd for t in self.closed), 2),
        }
