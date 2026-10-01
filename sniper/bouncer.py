"""The Bouncer: checks every new pair as soon as its launch tape is in, decides ENTER / WATCH / AVOID,
and paper-trades the result.

Three paper books run side by side with identical exits:
  bouncer  buys only the pairs the checks let in (ENTER)
  crowd    buys every pair with a big enough, spread-out crowd of buyers (the crowd rule alone)
  control  buys every pair that was checked, whatever the verdict
bouncer vs control is what the checks are worth; bouncer vs crowd is what the wallet, dev and supply
checks add on top of simply picking busy launches. Fills apply slippage and a fee; positions close on
take-profit, stop-loss or a time limit, priced from CoinGecko pool data every sweep. Nothing here
places real orders.
"""
import time

from core.client import CoinGeckoClient, CoinGeckoError, CreditBudgetExceeded

from . import analyze, checks
from .checks import BouncerConfig, Memory
from .config import SniperConfig
from .store import Store

PROFILE_TTL_S = 6 * 3600
MEMORY_TTL_S = 300


# ---- paper fills (pure) ----


def entry_fill(price: float, usd: float, b: BouncerConfig, reserve_usd: float | None = None) -> tuple[float, float]:
    """(fill price, token qty) for a $usd market buy at `price`, after price impact and fee.

    With the pool's liquidity known, impact follows a constant-product pool whose quote side holds
    half of `reserve_usd`: paying V into quote reserve Q moves the average fill to price * (Q + V) / Q.
    Without it, a flat `slippage_bps` is used."""
    if reserve_usd is not None and reserve_usd > 0:
        q = reserve_usd / 2
        fill = price * (q + usd) / q
    else:
        fill = price * (1 + b.slippage_bps / 10_000)
    qty = usd * (1 - b.fee_bps / 10_000) / fill
    return fill, qty


def exit_proceeds(qty: float, price: float, b: BouncerConfig, reserve_usd: float | None = None) -> float:
    """USD received for selling `qty` at `price`, after price impact and fee.

    Selling tokens worth V (at the current price) into quote reserve Q returns Q * V / (Q + V): a dead
    pool with $50 of liquidity left pays back at most $25, whatever its last price says."""
    v = qty * price
    if reserve_usd is not None:
        q = max(reserve_usd, 0) / 2
        v = q * v / (q + v) if q + v > 0 else 0.0
    else:
        v *= 1 - b.slippage_bps / 10_000
    return v * (1 - b.fee_bps / 10_000)


def exit_reason(entry_price: float, price: float | None, opened_ts: float, now: float, b: BouncerConfig) -> str | None:
    """Why a position should close now, or None. Uses the raw pool price move since entry."""
    if price is not None and entry_price:
        move = price / entry_price - 1
        if move >= b.take_profit_pct / 100:
            return "take_profit"
        if move <= -b.stop_loss_pct / 100:
            return "stop_loss"
    if now - opened_ts >= b.max_hold_min * 60:
        return "time"
    return None


# ---- the live bouncer ----


class Bouncer:
    def __init__(self, store: Store, cfg: SniperConfig):
        self.store = store
        self.cfg = cfg
        self.b = cfg.bouncer_config()
        self.memory = Memory()
        self._memory_ts = 0.0
        self.counts = {"ENTER": 0, "WATCH": 0, "AVOID": 0}

    def refresh_memory(self, force: bool = False):
        if force or time.time() - self._memory_ts > MEMORY_TTL_S:
            launches, rugs = self.store.deployer_history(self.b.rug_reserve_usd, creation_block_s=self.cfg.creation_block_s)
            self.memory = Memory(classes=self.store.load_classes(), packs=self.store.load_packs(), dev_launches=launches, dev_rugs=rugs,
                                 wallet_rugs=self.store.wallet_rug_memory(self.b.rug_reserve_usd))
            self._memory_ts = time.time()

    async def _token_info(self, client: CoinGeckoClient, chain: str, token: str | None) -> dict | None:
        if not token:
            return None
        d = await client.get(f"/onchain/networks/{chain}/tokens/{token}/info")
        return (d.get("data") or {}).get("attributes") or None

    async def _profiles(self, client: CoinGeckoClient, chain: str, wallets: list[str]) -> dict[str, dict]:
        profiles = self.store.wallet_lite(wallets, PROFILE_TTL_S)
        for w in wallets:
            if w in profiles:
                continue
            try:
                d = await client.get(f"/onchain/wallets/{w}/pnl", {"networks": chain})
            except CreditBudgetExceeded:
                raise
            except CoinGeckoError:
                continue
            a = (d.get("data") or {}).get("attributes") or {}
            total, realized = a.get("total_tokens"), a.get("total_realized_pnl_usd")
            realized = float(realized) if realized not in (None, "") else None
            self.store.save_wallet_lite(w, total, realized)
            profiles[w] = {"total_tokens": total, "realized_usd": realized}
        return profiles

    async def evaluate(self, client: CoinGeckoClient, chain: str, pool: dict, tape: dict, act: dict | None) -> checks.Verdict:
        """Runs the checks for one freshly captured launch, stores the verdict and opens paper positions.
        Call refresh_memory() once per sweep BEFORE capturing, so the memory never includes the launch
        being judged."""
        b = self.b
        raw = [{**t, "chain": chain, "pool": pool["pool"]} for t in tape["trades"]]
        trades = analyze.drop_fee_legs(raw, self.cfg)
        act = act or {}
        price = act.get("price_usd")
        reported_reserve = act.get("reserve_usd")
        reserve = checks.usable_reserve(reported_reserve, act.get("trades_m5"), b)
        supply = act.get("supply")
        mem = self.memory
        feat = dict(raw_trades=raw, created_ts=pool.get("created_ts"), creation_block_s=self.cfg.creation_block_s)

        f = checks.tape_features(trades, b, supply=supply, **feat)
        found = checks.stage0(f, mem, b, reserve)
        stage = 0
        go_on = not any(c.status == checks.FAIL for c in found) or checks.only_stand_in_dev_failed(found, f)
        if go_on and price is not None:
            try:
                info = await self._token_info(client, chain, pool.get("token"))
            except CreditBudgetExceeded:
                raise
            except CoinGeckoError:
                info = None
            developer = (info or {}).get("developer_address")
            if info:
                self.store.save_launch_info(chain, pool["pool"], {
                    "token": pool.get("token"), "developer": developer or None,
                    "developer_holding_pct": info.get("developer_holding_percentage"), "gt_score": info.get("gt_score"),
                    "holders": (info.get("holders") or {}).get("count"), "launchpad": info.get("launchpad_details"),
                })
            if developer:  # re-run the free checks with the real developer address
                f = checks.tape_features(trades, b, developer, supply, **feat)
                found = checks.stage0(f, mem, b, reserve)
            found += checks.stage1(info, b)
            stage = 1
        if stage == 1 and not any(c.status == checks.FAIL for c in found):
            profiles = await self._profiles(client, chain, checks.top_buyers(f, b))
            found += checks.stage2(profiles, b)
            stage = 2
        verdict = checks.decide(found, stage, f)
        verdict.features["price_usd"] = price
        verdict.features["reserve_usd"] = reserve
        verdict.features["reported_reserve_usd"] = reported_reserve
        fillable = price is not None and (reserve is None or reserve >= b.min_fill_reserve_usd) and (reported_reserve or 0) >= 0
        if reserve is None and reported_reserve is not None and reported_reserve < b.min_fill_reserve_usd and not act.get("trades_m5"):
            fillable = False  # genuinely tiny and quiet: nothing to buy into
        verdict.features["fillable"] = fillable
        self.store.save_verdict(chain, pool["pool"], verdict.to_dict(), price, reserve)
        self.counts[verdict.verdict] += 1

        now = time.time()
        if fillable:
            fill, qty = entry_fill(price, b.position_usd, b, reserve)
            self.store.open_position(chain, pool["pool"], "control", now, fill, b.position_usd, qty, price, reserve)
            if checks.crowd_only(f, b):
                self.store.open_position(chain, pool["pool"], "crowd", now, fill, b.position_usd, qty, price, reserve)
            if verdict.verdict == "ENTER":
                self.store.open_position(chain, pool["pool"], "bouncer", now, fill, b.position_usd, qty, price, reserve)
        return verdict

    async def manage_positions(self, client: CoinGeckoClient, chain: str, pools_multi) -> dict:
        """Marks every open paper position to the current pool price and closes the ones that hit an exit.
        A position whose time limit passed while the bot was not running (downtime, daily credit pause)
        closes at its last mark from before the limit, not at today's price."""
        open_rows = self.store.open_positions(chain)
        if not open_rows:
            return {"open": 0, "closed": 0}
        addresses = sorted({r["pool"] for r in open_rows})
        prices: dict[str, float] = {}
        reserves: dict[str, float | None] = {}
        for row in await pools_multi(client, chain, addresses):
            a = row.get("attributes") or {}
            p = analyze._f(a.get("base_token_price_usd"))
            if a.get("address") and p:
                snap = analyze.pool_snapshot(row)
                prices[a["address"]] = p
                reserves[a["address"]] = checks.usable_reserve(snap["reserve_usd"], snap["trades_m30"], self.b)
        now = time.time()
        closed = 0
        for r in open_rows:
            price = prices.get(r["pool"])
            reserve = reserves.get(r["pool"])
            if price is not None:
                self.store.mark_position(chain, r["pool"], r["book"], price, now, reserve)
            # take-profit / stop-loss are measured on the raw pool price move since entry
            stale = now - r["opened_ts"] > self.b.max_hold_min * 60 + 2 * self.cfg.interval_s
            if stale:  # the limit passed while the bot wasn't watching: use the last mark, not today's price
                reason, exit_price, exit_reserve = "time_stale", (r["last_price"] or 0.0), r.get("last_reserve")
                pnl = exit_proceeds(r["qty"], exit_price, self.b, exit_reserve) - r["usd"]
                self.store.close_position(chain, r["pool"], r["book"], now, exit_price, reason, round(pnl, 4))
                closed += 1
                continue
            reason = exit_reason(r.get("entry_raw_price") or r["entry_price"], price, r["opened_ts"], now, self.b)
            if reason:
                exit_price = price if price is not None else (r["last_price"] or 0.0)
                exit_reserve = reserve if price is not None else r.get("last_reserve")
                pnl = exit_proceeds(r["qty"], exit_price, self.b, exit_reserve) - r["usd"]
                self.store.close_position(chain, r["pool"], r["book"], now, exit_price, reason if price is not None else f"{reason}_no_price", round(pnl, 4))
                closed += 1
        self.store.commit()
        return {"open": len(open_rows) - closed, "closed": closed}


def simulate_path(candles: list, decision_ts: float, b: BouncerConfig, exit_reserve: float | None = None, end_price: float | None = None) -> dict | None:
    """Replays one paper trade on minute candles: buy at the last close before `decision_ts`, then the
    first candle that reaches the take-profit or the stop-loss closes it (stop first when a candle hits
    both; a stop fills at the stop price or the candle's close, whichever is lower, so gaps and pulled
    liquidity are not smoothed over). Otherwise it closes at the time limit, at `end_price` (the +60m
    snapshot) when given, through `exit_reserve` liquidity. Returns None if there was no price yet."""
    candles = sorted(candles)
    before = [c for c in candles if c[0] + 60 <= decision_ts]
    if not before:
        return None
    entry_raw = before[-1][4]
    if not entry_raw:
        return None
    fill, qty = entry_fill(entry_raw, b.position_usd, b)
    tp, sl = entry_raw * (1 + b.take_profit_pct / 100), entry_raw * (1 - b.stop_loss_pct / 100)
    deadline = decision_ts + b.max_hold_min * 60
    last_close = entry_raw
    for ts, _o, high, low, close, _v in (c for c in candles if c[0] >= decision_ts):
        if ts >= deadline:
            break
        if low is not None and low <= sl:
            price = min(sl, close)
            return {"entry_raw": entry_raw, "exit_price": price, "reason": "stop_loss", "minutes": (ts - decision_ts) / 60,
                    "pnl": exit_proceeds(qty, price, b) - b.position_usd}
        if high is not None and high >= tp:
            return {"entry_raw": entry_raw, "exit_price": tp, "reason": "take_profit", "minutes": (ts - decision_ts) / 60,
                    "pnl": exit_proceeds(qty, tp, b) - b.position_usd}
        last_close = close
    price = end_price if end_price is not None else last_close
    return {"entry_raw": entry_raw, "exit_price": price, "reason": "time", "minutes": b.max_hold_min,
            "pnl": exit_proceeds(qty, price, b, exit_reserve) - b.position_usd}


def book_stats(rows: list[dict], book: str, b: BouncerConfig, marks: dict | None = None) -> dict:
    """Realized and open PnL of one paper book."""
    rows = [r for r in rows if r["book"] == book]
    closed = [r for r in rows if r["closed_ts"] is not None]
    open_ = [r for r in rows if r["closed_ts"] is None]
    realized = sum(r["pnl_usd"] or 0 for r in closed)
    unreal = sum(exit_proceeds(r["qty"], (marks or {}).get(r["pool"], r["last_price"] or 0), b, r.get("last_reserve")) - r["usd"] for r in open_)
    wins = sum(1 for r in closed if (r["pnl_usd"] or 0) > 0)
    reasons: dict[str, int] = {}
    for r in closed:
        reasons[r["exit_reason"]] = reasons.get(r["exit_reason"], 0) + 1
    deployed = sum(r["usd"] for r in rows)
    return {
        "positions": len(rows),
        "closed": len(closed),
        "open": len(open_),
        "deployed_usd": round(deployed, 2),
        "realized_usd": round(realized, 2),
        "open_pnl_usd": round(unreal, 2),
        "pnl_usd": round(realized + unreal, 2),
        "return_pct": round(100 * (realized + unreal) / deployed, 2) if deployed else None,
        "win_rate": round(wins / len(closed), 3) if closed else None,
        "exits": reasons,
    }
