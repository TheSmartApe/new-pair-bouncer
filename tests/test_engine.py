import asyncio

from bot.engine import Engine
from bot.strategy import Strategy
from core.paper import Portfolio


class FakeClient:
    async def pool(self, chain, address):
        return {"attributes": {"base_token_price_usd": "11"}}


def test_engine_enters_and_reports_position():
    strategy = Strategy({"name": "test", "entry": {"budget_usd": 50}, "exits": {"take_profit_pct": 10}})
    engine = Engine(Portfolio(cash=1000, slippage_bps=0, fee_bps=0, max_position_pct=1, cooldown_s=0), strategy)
    ev = strategy.evaluate_from_record({"chain": "solana", "address": "pool-1", "symbol": "COIN", "price_usd": 10})
    decision = engine.try_enter(ev)
    assert decision["action"] == "entry"
    assert engine.open_pool_keys() == ["solana_pool-1"]
    assert engine.open_positions_view({"solana:pool-1": 11})[0]["symbol"] == "COIN"


def test_engine_monitor_closes_at_take_profit():
    strategy = Strategy({"name": "test", "entry": {"budget_usd": 50}, "exits": {"take_profit_pct": 10}})
    engine = Engine(Portfolio(cash=1000, slippage_bps=0, fee_bps=0, max_position_pct=1, cooldown_s=0), strategy)
    ev = strategy.evaluate_from_record({"chain": "solana", "address": "pool-1", "symbol": "COIN", "price_usd": 10})
    engine.try_enter(ev)
    decisions = asyncio.run(engine.monitor_open_positions(FakeClient()))
    assert decisions[0]["action"] == "exit"
    assert not engine.portfolio.positions
