"""Offline tests for the serial sniper analysis. No network calls."""
from sniper import analyze
from sniper.config import SniperConfig

T0 = 1_790_000_000.0


def _iso(ts):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(wallet, kind, block, ts, usd=10.0, amount=1000.0, tx=None):
    return {
        "kind": kind,
        "tx_from_address": wallet,
        "block_number": block,
        "block_timestamp": _iso(ts),
        "tx_hash": tx or f"0x{wallet[-4:]}{block}{kind}",
        "volume_in_usd": str(usd),
        "to_token_amount": str(amount) if kind == "buy" else "0.001",
        "from_token_amount": str(amount) if kind == "sell" else "0.001",
    }


def test_build_tape_offsets_from_first_trade_and_window_cut():
    rows = [
        _row("0xdev", "buy", 100, T0 + 2, usd=200),
        _row("0xaaa", "buy", 104, T0 + 3),
        _row("0xbbb", "sell", 110, T0 + 5),
        _row("0xlate", "buy", 900, T0 + 500),  # outside the 120s window
    ]
    tape = analyze.build_tape(rows, T0, 120)
    assert tape["launch_block"] == 100
    wallets = [t["wallet"] for t in tape["trades"]]
    assert wallets == ["0xdev", "0xaaa", "0xbbb"]
    aaa = tape["trades"][1]
    assert aaa["block_offset"] == 4 and aaa["sec_offset"] == 1.0


def test_build_tape_folds_multi_hop_rows_of_one_tx():
    rows = [_row("0xaaa", "buy", 100, T0, usd=5, amount=10, tx="0xt"), _row("0xaaa", "buy", 100, T0, usd=7, amount=20, tx="0xt")]
    tape = analyze.build_tape(rows, T0, 120)
    assert len(tape["trades"]) == 1
    assert tape["trades"][0]["usd"] == 12 and tape["trades"][0]["token_amount"] == 30


def _stored(chain, pool, tape):
    return [{**t, "chain": chain, "pool": pool} for t in tape["trades"]]


def _dataset():
    """Five launches. 0xs1 and 0xs2 snipe four of them together (same block in three);
    0xdev buys in the launch block of three; 0x4337bund is a bundler; 0xone snipes once; 0xslow buys late."""
    trades, pools = [], []
    plan = {
        "p1": [("0xdev", 0, 0), ("0xs1", 2, 1), ("0xs2", 2, 1), ("0x4337bund", 1, 1), ("0xslow", 50, 30)],
        "p2": [("0xdev", 0, 0), ("0xs1", 3, 1), ("0xs2", 3, 1), ("0x4337bund", 1, 1)],
        "p3": [("0xdev", 0, 0), ("0xs1", 1, 1), ("0xs2", 5, 2), ("0x4337bund", 2, 1), ("0xone", 2, 1)],
        "p4": [("0xother", 0, 0), ("0xs1", 4, 2), ("0xs2", 4, 2)],
        "p5": [("0xother", 0, 0), ("0xs1", 3, 2)],
    }
    for i, (pool, buys) in enumerate(plan.items()):
        created = T0 + i * 3600
        rows = [_row(w, "buy", 1000 * (i + 1) + blk, created + sec) for w, blk, sec in buys]
        if pool == "p1":
            rows.append(_row("0xs1", "sell", 1000 + 20, created + 12))
        tape = analyze.build_tape(rows, created, 120)
        trades += _stored("robinhood", pool, tape)
        pools.append({"chain": "robinhood", "pool": pool, "token": f"tok_{pool}", "name": pool, "created_ts": created, "status": "captured"})
    return trades, pools


def test_wallet_classes():
    trades, pools = _dataset()
    cfg = SniperConfig(snipe_s=10, min_launches=3)
    stats = analyze.wallet_stats(trades, pools, cfg)
    assert stats["0xs1"]["label"] == "serial_sniper" and stats["0xs1"]["launches"] == 5
    assert stats["0xs1"]["sold_in_window"] == 1 and stats["0xs1"]["round_trips"] == 1  # 1 of 5 is below the round-tripper rate
    assert stats["0xs2"]["label"] == "serial_sniper" and stats["0xs2"]["launches"] == 4
    assert stats["0xdev"]["label"] == "serial_launcher" and stats["0xdev"]["block0_launches"] == 3
    assert stats["0x4337bund"]["label"] == "bundler"
    assert stats["0xone"]["label"] == "one_off"
    assert "0xslow" not in stats  # bought 30s after the first trade: not a snipe at snipe_s=10


def test_thresholds_are_replayable():
    trades, pools = _dataset()
    stats = analyze.wallet_stats(trades, pools, SniperConfig(snipe_s=60, min_launches=1))
    assert stats["0xslow"]["label"] == "serial_sniper"


def test_packs_link_wallets_that_snipe_together():
    trades, pools = _dataset()
    cfg = SniperConfig(snipe_s=10, min_launches=3, pack_min_shared=3, pack_min_overlap=0.5)
    stats = analyze.wallet_stats(trades, pools, cfg)
    packs = analyze.find_packs(trades, stats, cfg)
    pack = next(p for p in packs if "0xs1" in p["members"])
    assert set(pack["members"]) >= {"0xs1", "0xs2"}
    assert pack["shared_launches"] >= 4
    assert 0 < pack["same_block_rate"] <= 1


def test_infrastructure_rate_cap():
    trades, pools = _dataset()
    cfg = SniperConfig(snipe_s=10, min_launches=3, max_launches_per_hour=0.5)  # 5 launches over 4h > 0.5/h
    stats = analyze.wallet_stats(trades, pools, cfg)
    assert stats["0xs1"]["label"] == "infrastructure"


def test_outcomes_compare_groups():
    trades, pools = _dataset()
    cfg = SniperConfig(snipe_s=10, min_launches=3, snapshot_ages_min=[60])
    stats = analyze.wallet_stats(trades, pools, cfg)
    # snipe price is 10 USD / 1000 tokens = 0.01; p1 at +60m trades at 0.001 (down 90%), p5 at 0.02
    snaps = [
        {"chain": "robinhood", "pool": "p1", "target_age_min": 60, "price_usd": 0.001, "trades_h1": 0, "reserve_usd": 50},
        {"chain": "robinhood", "pool": "p5", "target_age_min": 60, "price_usd": 0.02, "trades_h1": 8, "reserve_usd": 5000},
    ]
    outcomes = analyze.launch_outcomes(pools, trades, snaps, stats, cfg)
    p1 = next(o for o in outcomes if o["pool"] == "p1")
    assert p1["alive_60m"] is False and abs(p1["multiple_60m"] - 0.1) < 1e-9
    cmp = analyze.compare_outcomes(outcomes, 60)
    assert cmp["with"]["n"] == 2
    assert cmp["with"]["down_90_pct"] == 50.0


def test_parse_pool_strips_network_prefix():
    row = {
        "attributes": {"address": "0xpool", "name": "X / WETH", "pool_created_at": "2026-09-29T17:25:42Z"},
        "relationships": {"base_token": {"data": {"id": "robinhood_0xtok"}}, "quote_token": {"data": {"id": "robinhood_0xweth"}}, "dex": {"data": {"id": "pons-v2"}}},
    }
    p = analyze.parse_pool(row)
    assert p["token"] == "0xtok" and p["quote"] == "0xweth" and p["dex"] == "pons-v2" and p["created_ts"] > 0


def _add_launch(trades, pools, pool, created, buys, sells=()):
    rows = [_row(w, "buy", 5000 + blk, created + sec) for w, blk, sec in buys]
    rows += [_row(w, "sell", 5000 + blk, created + sec) for w, blk, sec in sells]
    tape = analyze.build_tape(rows, created, 120)
    trades += _stored("robinhood", pool, tape)
    pools.append({"chain": "robinhood", "pool": pool, "token": f"tok_{pool}", "name": pool, "created_ts": created, "status": "captured"})


def test_round_trippers_are_separated_from_snipers():
    trades, pools = [], []
    for i in range(4):
        created = T0 + i * 600
        _add_launch(
            trades, pools, f"q{i}", created,
            buys=[("0xdev", 0, 0), ("0xfarm1", 9, 1), ("0xfarm2", 9, 1), ("0xhold", 12, 2)],
            sells=[("0xfarm1", 40, 7), ("0xfarm2", 41, 8), ("0xhold", 400, 100)],  # 0xhold sells 98s later: not a round trip
        )
    stats = analyze.wallet_stats(trades, pools, SniperConfig(snipe_s=10, min_launches=3, roundtrip_max_s=30))
    assert stats["0xfarm1"]["label"] == "round_tripper" and stats["0xfarm1"]["round_trips"] == 4
    assert stats["0xfarm1"]["median_hold_in_window_s"] == 6.0
    assert stats["0xhold"]["label"] == "serial_sniper" and stats["0xhold"]["round_trips"] == 0
    assert stats["0xdev"]["label"] == "serial_launcher"


def test_packs_do_not_glue_a_hit_everything_sniper_to_a_subgroup():
    """0xbig snipes all 8 launches ~40 blocks after the farm; the farm pair shares 4. Jaccard and
    block timing must keep 0xbig out of the farm pack."""
    trades, pools = [], []
    for i in range(8):
        buys = [("0xbig", 45, 5)]
        if i < 4:
            buys += [("0xf1", 3, 1), ("0xf2", 3, 1)]
        _add_launch(trades, pools, f"r{i}", T0 + i * 600, buys=[("0xlaunch", 0, 0)] + buys)
    cfg = SniperConfig(snipe_s=10, min_launches=3)
    stats = analyze.wallet_stats(trades, pools, cfg)
    packs = analyze.find_packs(trades, stats, cfg)
    farm = next(p for p in packs if "0xf1" in p["members"])
    assert set(farm["members"]) == {"0xf1", "0xf2"}
    assert farm["same_block_rate"] == 1.0
    assert all("0xbig" not in p["members"] for p in packs)


def test_live_sql_classes_match_python(tmp_path):
    from sniper.collect import current_serial
    from sniper.store import Store

    trades, pools = [], []
    for i in range(4):
        _add_launch(
            trades, pools, f"s{i}", T0 + i * 600,
            buys=[("0xdev", 0, 0), ("0xfarm1", 9, 1), ("0xhold", 12, 2)],
            sells=[("0xfarm1", 40, 7), ("0xhold", 400, 100)],
        )
    cfg = SniperConfig(snipe_s=10, min_launches=3)
    store = Store(tmp_path / "t.db")
    for p in pools:
        store.add_pool("robinhood", {**p})
        tape_trades = [t for t in trades if t["pool"] == p["pool"]]
        store.save_capture("robinhood", p["pool"], {"launch_block": 5000, "launch_ts": p["created_ts"], "trades": tape_trades, "truncated": False})
    store.commit()
    live = current_serial(store, cfg)
    stats = analyze.wallet_stats(store.all_trades(), store.all_pools(), cfg)
    assert {w: v["label"] for w, v in live.items()} == {w: s["label"] for w, s in stats.items() if s["label"] in analyze.SERIAL_CLASSES}


def test_profitable_fast_flippers_stay_snipers():
    trades, pools = [], []
    for i in range(3):
        rows = [_row("0xdev", "buy", 6990, T0 + i * 600, usd=50)]
        rows += [_row("0xflip", "buy", 7000, T0 + i * 600 + 1, usd=100), _row("0xflip", "sell", 7040, T0 + i * 600 + 8, usd=150)]
        rows += [_row("0xbot", "buy", 7000, T0 + i * 600 + 1, usd=10), _row("0xbot", "sell", 7041, T0 + i * 600 + 8, usd=9)]
        tape = analyze.build_tape(rows, T0 + i * 600, 120)
        trades += _stored("robinhood", f"f{i}", tape)
        pools.append({"chain": "robinhood", "pool": f"f{i}", "token": "t", "name": "f", "created_ts": T0 + i * 600, "status": "captured"})
    stats = analyze.wallet_stats(trades, pools, SniperConfig(snipe_s=10, min_launches=3))
    assert stats["0xflip"]["label"] == "serial_sniper" and stats["0xflip"]["round_trip_cost_usd"] == -150.0
    assert stats["0xbot"]["label"] == "round_tripper" and stats["0xbot"]["round_trip_cost_usd"] == 3.0
