"""Loads a strategy YAML and evaluates one candidate against it, tier by tier, logging every check."""
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import filters
from .candidates import now_ts, top_trader_realized_pnl


@dataclass
class CheckResult:
    """One named filter's outcome, always with a human reason."""

    name: str
    passed: bool
    reason: str


@dataclass
class Evaluation:
    """Every check run against one candidate, and whether it survived all of them."""

    candidate: dict
    checks: list[CheckResult] = field(default_factory=list)
    token_info: dict | None = None
    best_trader_pnl_usd: float | None = None

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def as_dict(self) -> dict:
        return {
            "symbol": self.candidate.get("symbol"),
            "address": self.candidate.get("address"),
            "chain": self.candidate.get("chain"),
            "passed": self.passed,
            "checks": [{"name": c.name, "passed": c.passed, "reason": c.reason} for c in self.checks],
            # Full inputs, so `backtest --replay-scans` can re-run a strategy's filter logic offline, with no lookahead:
            # everything here was already observed at scan time.
            "candidate": self.candidate,
            "token_info": self.token_info,
            "best_trader_pnl_usd": self.best_trader_pnl_usd,
        }


class Strategy:
    """A named, YAML-configured screening + paper-trading rule set."""

    def __init__(self, data: dict):
        self.raw = data
        self.name = data.get("name", "unnamed-strategy")
        self.description = data.get("description", "")
        self.source = data.get("source", {}) or {}
        self.pool_filters = data.get("filters", {}) or {}
        self.smart_money = data.get("smart_money_gate", {}) or {}
        self.entry = data.get("entry", {}) or {}
        self.exits = data.get("exits", {}) or {}

    @classmethod
    def load(cls, path: str | Path) -> "Strategy":
        return cls(yaml.safe_load(Path(path).read_text()))

    @property
    def networks(self) -> list[str]:
        """Every network id this strategy's source scans: `source.networks`/`source.chains` (a list) if
        set, else the single `source.chain` (default 'solana'). Strategies can target any GeckoTerminal
        network or list of them -- there's no hard-coded chain allowlist here."""
        src = self.source
        nets = src.get("networks") or src.get("chains")
        if nets:
            return [str(n) for n in nets]
        return [str(src.get("chain", "solana"))]

    @property
    def _allow_unknown(self) -> set[str]:
        """Check names this strategy has opted into treating a missing value as a pass rather than a
        fail (`filters.allow_unknown: [liquidity, gt_score]`) -- see filters.check_min."""
        return set(self.pool_filters.get("allow_unknown", []) or [])

    @property
    def needs_token_info(self) -> bool:
        f = self.pool_filters
        return any(k in f for k in ("min_gt_score", "no_honeypot", "max_top10_holder_pct", "max_dev_holding_pct"))

    @property
    def needs_smart_money(self) -> bool:
        return bool(self.smart_money.get("enabled"))

    def evaluate_pool_tier(self, candidate: dict) -> Evaluation:
        """Cheap checks that use only the pool-list data already in hand: no extra credits spent."""
        ev = Evaluation(candidate=candidate)
        f = self.pool_filters
        now = now_ts()
        ev.checks.append(CheckResult("age", *filters.check_max_age(candidate.get("pool_created_at"), now, f.get("max_age_hours"))))
        allow_unknown = self._allow_unknown
        ev.checks.append(
            CheckResult(
                "liquidity",
                *filters.check_min(candidate.get("liquidity_usd"), f.get("min_liquidity_usd"), "liquidity", " USD", allow_unknown="liquidity" in allow_unknown),
            )
        )
        ev.checks.append(
            CheckResult(
                "volume_24h",
                *filters.check_min(candidate.get("volume_24h_usd"), f.get("min_volume_24h_usd"), "24h volume", " USD", allow_unknown="volume_24h" in allow_unknown),
            )
        )
        min_txns = f.get("min_txns_24h")
        txns = (candidate.get("buys_24h") or 0) + (candidate.get("sells_24h") or 0)
        ev.checks.append(CheckResult("txns_24h", *filters.check_min(txns, min_txns, "24h txns", allow_unknown="txns_24h" in allow_unknown)))
        if "min_price_change_pct_1h" in f:
            ev.checks.append(
                CheckResult("momentum_1h", *filters.check_min(candidate.get("price_change_pct_1h"), f.get("min_price_change_pct_1h"), "1h price change", "%"))
            )
        return ev

    def apply_token_info(self, ev: Evaluation, token_info: dict | None):
        """Adds the token_info-dependent checks once token_info has been fetched (or skipped)."""
        ev.token_info = token_info
        f = self.pool_filters
        info = token_info or {}
        if "min_gt_score" in f:
            ev.checks.append(
                CheckResult("gt_score", *filters.check_min(info.get("gt_score"), f.get("min_gt_score"), "GT Score", allow_unknown="gt_score" in self._allow_unknown))
            )
        if f.get("no_honeypot"):
            ev.checks.append(CheckResult("honeypot", *filters.check_honeypot(info.get("is_honeypot"), True)))
        if "max_top10_holder_pct" in f:
            ev.checks.append(CheckResult("top10_holders", *filters.check_max(info.get("top10_holder_pct"), f.get("max_top10_holder_pct"), "top-10 holders", "%")))
        if "max_dev_holding_pct" in f:
            ev.checks.append(CheckResult("dev_holding", *filters.check_max(info.get("dev_holding_pct"), f.get("max_dev_holding_pct"), "dev holding", "%")))

    def apply_smart_money(self, ev: Evaluation, traders: list[dict] | None):
        """Adds the smart-money gate once top_traders has been fetched (or skipped)."""
        self.apply_smart_money_pnl(ev, top_trader_realized_pnl(traders or []))

    def apply_smart_money_pnl(self, ev: Evaluation, best_trader_pnl_usd: float | None):
        """Same gate as apply_smart_money(), for callers (like backtest --replay-scans) that already have the PnL number."""
        ev.best_trader_pnl_usd = best_trader_pnl_usd
        threshold = self.smart_money.get("min_top_trader_pnl_usd")
        ev.checks.append(CheckResult("smart_money", *filters.check_smart_money(best_trader_pnl_usd, threshold)))

    def evaluate_from_record(self, candidate: dict, token_info: dict | None = None, best_trader_pnl_usd: float | None = None) -> Evaluation:
        """Recomputes a full Evaluation from already-observed inputs (no network calls): what `backtest --replay-scans` uses
        to test a strategy's CURRENT thresholds against previously recorded scan data."""
        ev = self.evaluate_pool_tier(candidate)
        if self.needs_token_info:
            self.apply_token_info(ev, token_info)
        if self.needs_smart_money:
            self.apply_smart_money_pnl(ev, best_trader_pnl_usd)
        return ev


def load_all(strategies_dir: str | Path = "strategies") -> dict[str, Strategy]:
    """Every *.yaml strategy in `strategies_dir`, keyed by its `name` field."""
    out = {}
    for p in sorted(Path(strategies_dir).glob("*.yaml")):
        s = Strategy.load(p)
        out[s.name] = s
    return out
