"""Offline tests for the pre-entry checks and the paper books. No network calls."""
from sniper import bouncer, checks
from sniper.checks import FAIL, PASS, UNKNOWN, WARN, BouncerConfig, Memory

T0 = 1_790_000_000.0
B = BouncerConfig(min_buyers=8, max_top3_buy_share=0.6, max_roundtrip_volume_share=0.35, warn_roundtrip_volume_share=0.2, max_bot_buyer_share=0.3, max_cluster_buyer_share=0.4, warn_cluster_buyer_share=0.2, warn_dev_launches=2, min_gt_score=40)  # the original, looser thresholds the fixtures were written for


def t(wallet, kind, block_offset, sec, usd=10.0, amount=1000.0, block_base=5000):
    return {"chain": "robinhood", "pool": "p", "tx_hash": f"0x{wallet}{block_offset}{kind}{sec}", "wallet": wallet, "kind": kind,
            "block": block_base + block_offset, "ts": T0 + sec, "block_offset": block_offset, "sec_offset": sec, "usd": usd, "token_amount": amount}


def organic_tape(n=12):
    rows = [t("dev", "buy", 0, 0, 50)]
    rows += [t(f"b{i}", "buy", 3 + 7 * i, 1 + i * 5, 20 + i) for i in range(n)]
    return rows


def by_key(found):
    return {c.key: c for c in found}


def test_clean_launch_passes_stage0():
    f = checks.tape_features(organic_tape(), B, created_ts=T0)
    found = by_key(checks.stage0(f, Memory(), B, reserve_usd=12_000))
    assert found["wash_trading"].status == PASS
    assert found["known_bots"].status == PASS
    assert found["entity_cluster"].status == PASS
    assert found["dev_sold"].status == PASS
    assert found["buyers"].status == PASS and found["liquidity"].status == PASS
    assert checks.decide(list(found.values()), 0).verdict == "ENTER"


def test_wash_trading_fails():
    rows = organic_tape()
    for i in range(6):  # six wallets buy big and dump within seconds
        rows += [t(f"w{i}", "buy", 2, 1, 200), t(f"w{i}", "sell", 40, 8, 195)]
    f = checks.tape_features(rows, B)
    assert f["roundtrip_volume_share"] > B.max_roundtrip_volume_share
    found = by_key(checks.stage0(f, Memory(), B, reserve_usd=12_000))
    assert found["wash_trading"].status == FAIL
    assert checks.decide(list(found.values()), 0).verdict == "AVOID"


def test_known_bots_and_known_cluster_fail():
    rows = organic_tape(8) + [t(f"bot{i}", "buy", 9, 1, 5) for i in range(8)]
    mem = Memory(
        classes={f"bot{i}": {"label": "round_tripper", "launches": 18} for i in range(8)},
        packs={f"bot{i}": {"pack_id": "P1", "size": 15, "shared_launches": 18} for i in range(8)},
    )
    f = checks.tape_features(rows, B)
    found = by_key(checks.stage0(f, mem, B, reserve_usd=12_000))
    assert found["known_bots"].status == FAIL
    assert found["entity_cluster"].status == FAIL and "known cluster" in found["entity_cluster"].reason


def test_unknown_same_block_cohort_is_a_cluster():
    rows = organic_tape(6) + [t(f"c{i}", "buy", 4, 1, 15) for i in range(6)]  # six fresh wallets, same block
    f = checks.tape_features(rows, B)
    assert len(f["cohort"]) >= 6
    found = by_key(checks.stage0(f, Memory(), B, reserve_usd=12_000))
    assert found["entity_cluster"].status in (WARN, FAIL) and "same block" in found["entity_cluster"].reason


def test_dev_sold_fails_with_token_info_developer():
    rows = organic_tape() + [t("realdev", "buy", 1, 1, 300), t("realdev", "sell", 30, 20, 280)]
    f = checks.tape_features(rows, B, developer="REALDEV")
    found = by_key(checks.stage0(f, Memory(), B, reserve_usd=12_000))
    assert f["dev"] == "realdev" and found["dev_sold"].status == FAIL


def test_serial_launcher_warns_and_deployer_rugs_fail():
    f = checks.tape_features(organic_tape(), B, developer="dev")
    found = by_key(checks.stage0(f, Memory(dev_launches={"dev": 9}), B, reserve_usd=12_000))
    assert found["serial_launcher"].status == WARN  # launching a lot is not, alone, a reason to stay out
    assert found["deployer_rugs"].status == PASS
    found = by_key(checks.stage0(f, Memory(dev_launches={"dev": 9}, dev_rugs={"dev": 2}), B, reserve_usd=12_000))
    assert found["deployer_rugs"].status == FAIL and "2 earlier pools" in found["deployer_rugs"].reason


def test_supply_grab():
    # early buyers bought 300M tokens of a 1B supply and sold 20M: they still hold 28%
    rows = organic_tape()
    rows += [t("whale", "buy", 2, 1, 500, amount=200_000_000), t("whale2", "buy", 2, 1, 200, amount=100_000_000), t("whale2", "sell", 40, 9, 20, amount=20_000_000)]
    f = checks.tape_features(rows, B, supply=1_000_000_000)
    assert f["early_supply_share"] == round(280_013_000 / 1e9, 4)  # 280M from the two whales + 13 x 1,000 from the others
    found = by_key(checks.stage0(f, Memory(), B, reserve_usd=12_000))
    assert found["supply_grab"].status == FAIL and found["top3_supply"].status == WARN
    f = checks.tape_features(organic_tape(), B)  # supply unknown
    assert by_key(checks.stage0(f, Memory(), B, reserve_usd=12_000))["supply_grab"].status == UNKNOWN


def test_thin_launch_fails_on_buyers_and_liquidity():
    f = checks.tape_features(organic_tape(3), B)
    found = by_key(checks.stage0(f, Memory(), B, reserve_usd=800))
    assert found["buyers"].status == FAIL and found["liquidity"].status == FAIL


def test_stage1_token_info():
    found = by_key(checks.stage1({"is_honeypot": "unknown", "gt_score": 18.7, "developer_holding_percentage": "35.0",
                                  "holders": {"count": None, "distribution_percentage": None}}, B))
    assert found["honeypot"].status == UNKNOWN
    assert found["gt_score"].status == WARN
    assert found["dev_holding"].status == FAIL
    assert "top10_holders" not in found
    assert by_key(checks.stage1({"is_honeypot": True}, B))["honeypot"].status == FAIL
    assert checks.stage1(None, B)[0].status == UNKNOWN


def test_stage2_fresh_wallets():
    profiles = {f"w{i}": {"total_tokens": 1, "realized_usd": 0} for i in range(4)}
    profiles["pro"] = {"total_tokens": 400, "realized_usd": 25_000}
    found = by_key(checks.stage2(profiles, B))
    assert found["fresh_wallets"].status == FAIL
    assert found["proven_traders"].value == 1


def test_verdict_rules():
    w = checks.Check("a", "x", WARN, "")
    p = checks.Check("b", "x", PASS, "")
    f = checks.Check("c", "x", FAIL, "")
    assert checks.decide([p, w], 1).verdict == "ENTER"
    assert checks.decide([w, w], 1).verdict == "WATCH"
    assert checks.decide([p, f], 1).verdict == "AVOID"


def test_paper_fills_and_exits():
    b = BouncerConfig(slippage_bps=300, fee_bps=100, take_profit_pct=100, stop_loss_pct=50, max_hold_min=60)
    fill, qty = bouncer.entry_fill(0.01, 100, b)
    assert abs(fill - 0.0103) < 1e-12 and abs(qty - 99 / 0.0103) < 1e-6
    assert bouncer.exit_reason(0.01, 0.021, T0, T0 + 60, b) == "take_profit"
    assert bouncer.exit_reason(0.01, 0.0049, T0, T0 + 60, b) == "stop_loss"
    assert bouncer.exit_reason(0.01, 0.012, T0, T0 + 3600, b) == "time"
    assert bouncer.exit_reason(0.01, 0.012, T0, T0 + 600, b) is None
    # a round trip at an unchanged price loses slippage and fees on both legs
    assert bouncer.exit_proceeds(qty, 0.01, b) < 100


def test_book_stats():
    b = BouncerConfig()
    rows = [
        {"book": "bouncer", "pool": "a", "usd": 100, "qty": 1, "closed_ts": 1, "pnl_usd": 50, "exit_reason": "take_profit", "last_price": None},
        {"book": "bouncer", "pool": "b", "usd": 100, "qty": 1, "closed_ts": 1, "pnl_usd": -60, "exit_reason": "stop_loss", "last_price": None},
        {"book": "control", "pool": "a", "usd": 100, "qty": 1, "closed_ts": 1, "pnl_usd": 50, "exit_reason": "take_profit", "last_price": None},
    ]
    s = bouncer.book_stats(rows, "bouncer", b)
    assert s["positions"] == 2 and s["realized_usd"] == -10 and s["win_rate"] == 0.5 and s["exits"] == {"take_profit": 1, "stop_loss": 1}


def test_bouncer_config_rejects_typos():
    import pytest

    with pytest.raises(ValueError):
        BouncerConfig.from_dict({"min_buyerz": 3})


# ---- fixes from the adversarial review ----


def test_fallback_dev_needs_a_single_creation_block_buyer():
    tape = organic_tape()
    assert checks.fallback_dev(tape, T0) == "dev"
    assert checks.fallback_dev(tape, T0 - 60) is None  # trading opened a minute after creation: block 0 is a sniper
    two = tape + [t("other", "buy", 0, 0, 40)]
    assert checks.fallback_dev(two, T0) is None  # two block-0 buyers: no reliable stand-in
    f = checks.tape_features(organic_tape() + [t("dev", "sell", 30, 20, 40)], B, created_ts=T0 - 60)
    assert f["dev"] is None and not f["dev_sold"]


def test_only_stand_in_dev_failed_triggers_token_info():
    f = checks.tape_features(organic_tape() + [t("dev", "sell", 30, 20, 40)], B, created_ts=T0)
    found = checks.stage0(f, Memory(), B, reserve_usd=12_000)
    assert [c.key for c in found if c.status == FAIL] == ["dev_sold"]
    assert checks.only_stand_in_dev_failed(found, f)
    f2 = checks.tape_features(organic_tape() + [t("dev", "sell", 30, 20, 40)], B, developer="dev", created_ts=T0)
    assert not checks.only_stand_in_dev_failed(checks.stage0(f2, Memory(), B, reserve_usd=12_000), f2)


def test_usable_reserve_and_rug_rule():
    from sniper.store import is_rug

    assert checks.usable_reserve(0.0, 58, B) is None          # live v4 pool reported at $0: unknown, not empty
    assert checks.usable_reserve(2.1e-13, 0, B) == 2.1e-13      # quiet and empty: the liquidity is really gone
    assert checks.usable_reserve(40.0, 12, B) is None         # tiny but still trading: don't trust it
    assert checks.usable_reserve(40.0, 0, B) == 40.0          # tiny and quiet: really empty
    assert checks.usable_reserve(25_000.0, 30, B) == 25_000.0
    assert is_rug(8.3, 0, 100) and not is_rug(8.3, 266, 100) and not is_rug(8.3, None, 100)


def test_unknown_liquidity_is_not_a_fail():
    f = checks.tape_features(organic_tape(), B, created_ts=T0)
    assert by_key(checks.stage0(f, Memory(), B, reserve_usd=None))["liquidity"].status == UNKNOWN


def test_supply_grab_nets_same_tx_legs_from_raw_rows():
    raw = organic_tape() + [
        t("w", "buy", 2, 1, 300, amount=320_000_000),
        {**t("w", "sell", 2, 1, 280, amount=300_000_000), "tx_hash": "0xw2buy1"},  # same tx as the buy
    ]
    raw[-2]["tx_hash"] = "0xw2buy1"
    from sniper import analyze
    from sniper.config import SniperConfig

    clean = analyze.drop_fee_legs(raw, SniperConfig())
    f_clean_only = checks.tape_features(clean, B, supply=1e9)
    f = checks.tape_features(clean, B, supply=1e9, raw_trades=raw)
    assert f_clean_only["early_supply_share"] > 0.3          # the collapsed row alone overstates what w holds
    assert f["early_supply_share"] < 0.03                    # net of the same-tx sell: 20M + the small buyers


def test_simulate_path_ignores_prices_before_the_decision():
    b = BouncerConfig(take_profit_pct=100, stop_loss_pct=50, max_hold_min=60)
    decision = T0 + 330
    candles = [
        [T0 + 240, 1.0, 1.0, 1.0, 1.0, 10],
        [T0 + 300, 1.0, 1.0, 0.2, 1.0, 10],   # straddles the decision: its low printed before entry
        [T0 + 360, 1.0, 1.1, 0.9, 1.0, 10],
    ]
    trade = bouncer.simulate_path(candles, decision, b, exit_reserve=None, end_price=1.0)
    assert trade["reason"] == "time" and trade["entry_raw"] == 1.0
    candles.append([T0 + 420, 1.0, 1.0, 0.3, 0.3, 10])
    assert bouncer.simulate_path(candles, decision, b, None, 1.0)["reason"] == "stop_loss"


def test_rug_ring_fails_on_an_early_buyer_with_a_rug_heavy_past():
    tape = organic_tape() + [t("ringer", "buy", 4, 3, 30)]
    f = checks.tape_features(tape, B, created_ts=T0)
    assert "ringer" in f["early_wallets"]
    found = by_key(checks.stage0(f, Memory(wallet_rugs={"ringer": (5, 3)}), B, reserve_usd=12_000))
    assert found["rug_ring"].status == FAIL and "3 of its 5" in found["rug_ring"].reason
    found = by_key(checks.stage0(f, Memory(wallet_rugs={"ringer": (5, 1)}), B, reserve_usd=12_000))  # 20%: under the bar
    assert found["rug_ring"].status == PASS
    late = organic_tape() + [t("ringer", "buy", 400, 90, 30)]  # bought 90s in: not an early buyer
    f = checks.tape_features(late, B, created_ts=T0)
    assert by_key(checks.stage0(f, Memory(wallet_rugs={"ringer": (5, 5)}), B, reserve_usd=12_000))["rug_ring"].status == PASS


def test_pulled_pool_pays_back_almost_nothing():
    b = BouncerConfig()
    fill, qty = bouncer.entry_fill(0.01, 100, b, reserve_usd=8_000)
    gone = checks.usable_reserve(0.0, 0, b)
    assert gone == 0.0 and bouncer.exit_proceeds(qty, 0.03, b, gone) == 0.0  # the last price says 3x; the pool is empty
