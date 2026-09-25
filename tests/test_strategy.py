from pathlib import Path

from bot.candidates import normalize_pool, normalize_token_info, top_trader_realized_pnl
from bot.filters import check_honeypot, check_max, check_max_age, check_min, check_smart_money
from bot.strategy import Strategy


def test_candidate_and_token_normalization():
    candidate = normalize_pool(
        {
            "attributes": {
                "address": "pool-1",
                "name": "COIN / USDC",
                "reserve_in_usd": "12000",
                "volume_usd": {"h24": "9000"},
                "transactions": {"h24": {"buys": 10, "sells": 8}},
            },
            "relationships": {"base_token": {"data": {"id": "solana_token-1"}}},
        },
        "solana",
    )
    assert candidate["address"] == "pool-1"
    assert candidate["symbol"] == "COIN"
    assert candidate["liquidity_usd"] == 12000
    info = normalize_token_info({"gt_score": "75", "is_honeypot": False, "holders": {"top_10_holder_percentage": "22"}})
    assert info["gt_score"] == 75
    assert info["top10_holder_pct"] == 22
    assert top_trader_realized_pnl([{"realized_pnl_usd": "12"}, {"realized_pnl_usd": 20}]) == 20


def test_filters_are_explicit_about_unknown_values():
    assert check_min(10, 5, "liquidity")[0]
    assert not check_min(None, 5, "liquidity")[0]
    assert check_max(None, 5, "holders")[0]
    assert not check_honeypot(True, True)[0]
    assert check_honeypot(None, True)[0]
    assert check_smart_money(6000, 5000)[0]
    assert not check_smart_money(None, 5000)[0]
    assert check_max_age(None, 0, None)[0]


def test_yaml_strategy_replays_current_thresholds_without_network():
    strategy = Strategy.load(Path("strategies/trending-momentum.yaml"))
    candidate = {
        "chain": "solana",
        "address": "pool-1",
        "symbol": "COIN",
        "liquidity_usd": 100_000,
        "volume_24h_usd": 200_000,
        "buys_24h": 100,
        "sells_24h": 80,
        "price_change_pct_1h": 8,
    }
    evaluation = strategy.evaluate_from_record(candidate, {"is_honeypot": False, "top10_holder_pct": 20})
    assert evaluation.passed
    assert all(check.reason for check in evaluation.checks)
