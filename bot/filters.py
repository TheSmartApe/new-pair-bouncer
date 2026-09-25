"""Named filter checks. Every one returns (passed: bool, reason: str) so a scan can log why."""


def check_max_age(pool_created_ts: float | None, now_ts: float, max_age_hours: float | None):
    """Pool age <= max_age_hours."""
    if max_age_hours is None:
        return True, "age: no limit set"
    if pool_created_ts is None:
        return False, "age: pool_created_at unknown"
    age_h = (now_ts - pool_created_ts) / 3600
    if age_h <= max_age_hours:
        return True, f"age {age_h:.1f}h <= {max_age_hours}h"
    return False, f"age {age_h:.1f}h > {max_age_hours}h"


def check_min(value: float | None, threshold: float | None, label: str, unit: str = ""):
    """value >= threshold."""
    if threshold is None:
        return True, f"{label}: no minimum set"
    if value is None:
        return False, f"{label}: unknown"
    if value >= threshold:
        return True, f"{label} {value:,.0f}{unit} >= {threshold:,.0f}{unit}"
    return False, f"{label} {value:,.0f}{unit} < {threshold:,.0f}{unit}"


def check_max(value: float | None, threshold: float | None, label: str, unit: str = ""):
    """value <= threshold."""
    if threshold is None:
        return True, f"{label}: no maximum set"
    if value is None:
        return True, f"{label}: unknown, allowing"
    if value <= threshold:
        return True, f"{label} {value:,.1f}{unit} <= {threshold:,.1f}{unit}"
    return False, f"{label} {value:,.1f}{unit} > {threshold:,.1f}{unit}"


def check_honeypot(is_honeypot, required: bool):
    """Rejects a flagged honeypot, unless the strategy doesn't require the check."""
    if not required:
        return True, "honeypot check: not required by this strategy"
    if is_honeypot is None:
        return True, "honeypot check: unknown for this chain/token, allowing"
    if not is_honeypot:
        return True, "not flagged as a honeypot"
    return False, "flagged as a honeypot"


def check_smart_money(best_trader_pnl_usd: float | None, threshold: float | None):
    """A token's top traders show at least threshold USD of realized PnL."""
    if threshold is None:
        return True, "smart-money gate: no minimum set"
    if best_trader_pnl_usd is None:
        return False, "smart-money gate: no top-trader PnL data"
    if best_trader_pnl_usd >= threshold:
        return True, f"top trader realized PnL ${best_trader_pnl_usd:,.0f} >= ${threshold:,.0f}"
    return False, f"top trader realized PnL ${best_trader_pnl_usd:,.0f} < ${threshold:,.0f}"
