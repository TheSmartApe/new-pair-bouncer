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


def test_strategy_networks_supports_single_chain_and_a_list():
    single = Strategy({"name": "s", "source": {"chain": "base"}})
    assert single.networks == ["base"]
    multi = Strategy({"name": "m", "source": {"networks": ["solana", "base", "eth"]}})
    assert multi.networks == ["solana", "base", "eth"]
    default = Strategy({"name": "d", "source": {}})
    assert default.networks == ["solana"]


def test_allow_unknown_lets_a_missing_value_pass_only_when_opted_in():
    strategy = Strategy.load(Path("strategies/new-launch-sniff.yaml"))
    # liquidity_usd and gt_score are both None (not indexed yet on a brand-new pool); this strategy
    # opted both checks into `allow_unknown`, so a missing value should not by itself fail them.
    candidate = {
        "chain": "solana",
        "address": "pool-1",
        "symbol": "COIN",
        "liquidity_usd": None,
        "volume_24h_usd": 500,
        "buys_24h": 3,
        "sells_24h": 2,
        "pool_created_at": None,
    }
    ev = strategy.evaluate_from_record(candidate, token_info=None)
    liquidity_check = next(c for c in ev.checks if c.name == "liquidity")
    gt_score_check = next(c for c in ev.checks if c.name == "gt_score")
    assert liquidity_check.passed and "allowing" in liquidity_check.reason
    assert gt_score_check.passed and "allowing" in gt_score_check.reason

    # A strategy that does NOT opt a field into allow_unknown still fails on a missing value.
    strict = Strategy.load(Path("strategies/trending-momentum.yaml"))
    strict_candidate = {**candidate, "volume_24h_usd": None}
    strict_ev = strict.evaluate_from_record(strict_candidate, token_info=None)
    strict_volume_check = next(c for c in strict_ev.checks if c.name == "volume_24h")
    assert not strict_volume_check.passed
