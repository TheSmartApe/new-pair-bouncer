"""Deterministic wallet profiling shared by every starter: FIFO PnL, bot heuristics, labels, copyability."""
import statistics
from collections import defaultdict, deque
from datetime import datetime, timezone

STABLES = {"USDC", "USDT", "DAI", "USDG", "USDS", "USDE", "PYUSD", "FDUSD", "TUSD", "USD1", "USDC.E", "USDBC", "LUSD", "FRAX", "GHO", "RLUSD"}


def _f(x, default=None):
    """float(x), or default if x is missing or not a number."""
    if x is None or x == "":
        return default
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _ts(value) -> float | None:
    """A unix timestamp or an ISO8601 string, normalized to unix seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# ---- FIFO matching over a wallet's own trade history ----


def normalize_trades(rows: list[dict]) -> list[dict]:
    """Onchain wallet-trades API rows -> {token, kind, qty, usd, ts}, oldest first."""
    out = []
    for r in rows:
        kind = r.get("kind")
        if kind == "buy":
            token, qty = r.get("to_token_address"), _f(r.get("to_token_amount"), 0.0)
        elif kind == "sell":
            token, qty = r.get("from_token_address"), _f(r.get("from_token_amount"), 0.0)
        else:
            continue
        ts = _ts(r.get("block_timestamp"))
        if ts is None or not qty:
            continue
        out.append({"token": token, "kind": kind, "qty": qty, "usd": _f(r.get("volume_in_usd"), 0.0) or 0.0, "ts": ts})
    return sorted(out, key=lambda t: t["ts"])


def fifo_matches(trades: list[dict]) -> list[dict]:
    """Matches each sell against the earliest unmatched buy quantity of the same token."""
    lots: dict = defaultdict(deque)
    closed = []
    for t in trades:
        if t["kind"] == "buy":
            price = t["usd"] / t["qty"] if t["qty"] else 0.0
            lots[t["token"]].append({"qty": t["qty"], "price": price, "ts": t["ts"]})
            continue
        remaining = t["qty"]
        sell_price = t["usd"] / t["qty"] if t["qty"] else 0.0
        while remaining > 1e-12 and lots[t["token"]]:
            lot = lots[t["token"]][0]
            matched = min(lot["qty"], remaining)
            closed.append(
                {
                    "token": t["token"],
                    "qty": matched,
                    "buy_usd": matched * lot["price"],
                    "sell_usd": matched * sell_price,
                    "pnl_usd": matched * (sell_price - lot["price"]),
                    "hold_s": max(t["ts"] - lot["ts"], 0),
                    "opened_ts": lot["ts"],
                    "closed_ts": t["ts"],
                }
            )
            lot["qty"] -= matched
            remaining -= matched
            if lot["qty"] <= 1e-12:
                lots[t["token"]].popleft()
    return closed


def match_metrics(closed: list[dict]) -> dict:
    """Win rate, avg hold, median trade USD, and realized PnL from a wallet's own FIFO-matched trades."""
    if not closed:
        return {"trades": 0, "win_rate": None, "avg_hold_s": None, "median_trade_usd": None, "realized_pnl_usd": 0.0}
    wins = sum(1 for c in closed if c["pnl_usd"] > 0)
    return {
        "trades": len(closed),
        "win_rate": round(wins / len(closed), 3),
        "avg_hold_s": round(statistics.mean(c["hold_s"] for c in closed), 0),
        "median_trade_usd": round(statistics.median(c["sell_usd"] for c in closed), 2),
        "realized_pnl_usd": round(sum(c["pnl_usd"] for c in closed), 2),
    }


# ---- lifetime PnL and recent-activity summaries, from the wallet endpoints ----


def pnl_features(attrs: dict | None) -> dict:
    """Summarizes GET /onchain/wallets/{address}/pnl into lifetime win rate, concentration, and volume."""
    if not attrs:
        return {"available": False}
    stats = attrs.get("token_stats") or []
    sold = [_f(s.get("realized_pnl_usd"), 0.0) for s in stats if (s.get("total_sell_count") or 0) > 0]
    wins = [r for r in sold if r > 0]
    buys = sum(s.get("total_buy_count") or 0 for s in stats)
    sells = sum(s.get("total_sell_count") or 0 for s in stats)
    return {
        "available": True,
        "tokens_traded": attrs.get("total_tokens"),
        "lifetime_realized_pnl_usd": round(_f(attrs.get("total_realized_pnl_usd"), 0.0), 0),
        "lifetime_unrealized_pnl_usd": round(_f(attrs.get("total_unrealized_pnl_usd"), 0.0), 0),
        "win_rate_tokens": round(len(wins) / len(sold), 3) if sold else None,
        "total_buys": buys,
        "total_sells": sells,
    }


def activity_features(trades: list[dict], now: float | None = None) -> dict:
    """Trading cadence from a wallet's most recent trades: per-day rate, buy share, days since last trade."""
    now = now or datetime.now(timezone.utc).timestamp()
    stamps = sorted(t for t in (_ts(x.get("block_timestamp")) for x in trades) if t)
    if not stamps:
        return {"recent_trades": 0, "recent_trades_per_day": 0, "days_since_last_trade": None}
    span_days = max((stamps[-1] - stamps[0]) / 86400, 1 / 24)
    buys = sum(1 for x in trades if x.get("kind") == "buy")
    return {
        "recent_trades": len(stamps),
        "recent_trades_per_day": round(len(stamps) / span_days, 1),
        "recent_buy_share": round(buys / len(trades), 2) if trades else None,
        "days_since_last_trade": round((now - stamps[-1]) / 86400, 1),
    }


def likely_bot_row(row: dict) -> bool:
    """Cheap pre-filter for candidate lists: thousands of trades in a single token."""
    return (row.get("total_buy_count") or 0) + (row.get("total_sell_count") or 0) > 1500


# ---- labels + copyability ----


def label_wallet(metrics: dict, pnl: dict, activity: dict) -> str:
    """One rule-based label, checked in priority order: bot_like, dormant, proven_trader, one_hit, whale, flipper."""
    if (activity.get("recent_trades_per_day") or 0) > 50:
        return "bot_like"
    days_idle = activity.get("days_since_last_trade")
    if metrics["trades"] == 0:
        return "dormant" if (days_idle or 0) > 14 else "one_hit"
    if metrics["trades"] >= 5 and (metrics["win_rate"] or 0) >= 0.6 and metrics["realized_pnl_usd"] > 0:
        return "proven_trader"
    if metrics["trades"] == 1:
        return "one_hit"
    if days_idle and days_idle > 14:
        return "dormant"
    if (pnl.get("lifetime_realized_pnl_usd") or 0) > 100_000:
        return "whale"
    if (activity.get("recent_trades_per_day") or 0) > 10:
        return "flipper"
    return "flipper"


def copyability(median_trade_usd: float | None, trades_per_day: float | None, budget_usd: float) -> int:
    """0-100: can a budget_usd account actually mirror this wallet's typical trade size and pace?"""
    if not median_trade_usd or budget_usd <= 0:
        return 0
    size_ratio = budget_usd / median_trade_usd
    size_score = 100 if size_ratio >= 1 else max(0, round(size_ratio * 100))
    freq = trades_per_day or 0
    freq_score = 100 if freq <= 5 else max(0, round(100 - (freq - 5) * 5))
    return round(size_score * 0.7 + freq_score * 0.3)
