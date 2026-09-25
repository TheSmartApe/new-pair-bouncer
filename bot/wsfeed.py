"""Optional WebSocket price feed for open positions (Basic+ plans). REST polling always still covers price checks;
this just makes reactions faster when the plan has it. Best-effort: any failure here never stops the bot."""
from core.ws import CoinGeckoStream


class WsFeed:
    """Subscribes G3 (OnchainOHLCV, 1s candles) for whatever pools `pools()` returns; exposes the latest close seen."""

    def __init__(self, pools_fn):
        self.pools_fn = pools_fn
        self.prices: dict[str, float] = {}
        self._stream: CoinGeckoStream | None = None

    def _on_event(self, channel: str, message: dict):
        if channel != "G3":
            return
        data = message.get("data") or message
        pool_key = data.get("id") or data.get("pool_address") or data.get("network_id:pool_address")
        close = data.get("c") or data.get("close") or data.get("price_in_usd")
        if pool_key and close:
            try:
                self.prices[str(pool_key)] = float(close)
            except (TypeError, ValueError):
                pass

    def start(self):
        self._stream = CoinGeckoStream(self._on_event)
        self._stream.start()

    async def resync(self):
        """Re-subscribes to whatever pools are currently open, dropping ones that closed."""
        if not self._stream:
            return
        pools = self.pools_fn()
        await self._stream.set_pools(trade_pools=[], ohlcv_pools=pools)

    async def stop(self):
        if self._stream:
            await self._stream.stop()

    @property
    def status(self) -> str:
        return self._stream.status if self._stream else "idle"

    @property
    def credits_used(self) -> float:
        return self._stream.credits_used if self._stream else 0.0
