"""Offline tests for the serial sniper analysis. No network calls."""
from datetime import datetime, timezone

from sniper import analyze
from sniper.config import SniperConfig

T0 = 1_790_000_000.0
CFG = SniperConfig(snipe_s=10, min_launches=3)


def _iso(ts):
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


class Launches:
    """Builds recorded launches the way the collector stores them."""

    def __init__(self):
        self.trades, self.pools = [], []

    def add(self, pool, created, rows, token=None, status="captured", truncated=0):
        tape = analyze.build_tape(rows, created, 120, bool(truncated))
        self.trades += [{**t, "chain": "robinhood", "pool": pool} for t in tape["trades"]]
        self.pools.append(
            {"chain": "robinhood", "pool": pool, "token": token or f"tok_{pool}", "name": f"{pool} / WETH", "created_ts": created,
             "launch_ts": tape["launch_ts"], "status": status, "truncated": truncated}
        )

    def prepared(self, cfg=CFG):
        return analyze.prepare(self.pools, self.trades, cfg)

    def stats(self, cfg=CFG, info=None):
        pools, trades = self.prepared(cfg)
        return analyze.wallet_stats(trades, pools, cfg, info=info)


def buys(created, *spec, block_base=1000):
    """spec: (wallet, block_offset, sec_offset[, usd])"""
    out = []
    for s in spec:
        w, blk, sec = s[:3]
        usd = s[3] if len(s) > 3 else 10.0
        out.append(_row(w, "buy", block_base + blk, created + sec, usd=usd))
    return out


def sells(created, *spec, block_base=1000):
    out = []
    for s in spec:
        w, blk, sec = s[:3]
        usd = s[3] if len(s) > 3 else 10.0
        out.append(_row(w, "sell", block_base + blk, created + sec, usd=usd))
    return out


# ---- tape building ----


def test_build_tape_offsets_from_first_trade_and_window_cut():
    rows = buys(T0, ("0xdev", 0, 2, 200), ("0xaaa", 4, 3)) + sells(T0, ("0xbbb", 10, 5)) + buys(T0, ("0xlate", 900, 500))
    tape = analyze.build_tape(rows, T0, 120)
    assert tape["launch_block"] == 1000
    assert [t["wallet"] for t in tape["trades"]] == ["0xdev", "0xaaa", "0xbbb"]
    assert tape["trades"][1]["block_offset"] == 4 and tape["trades"][1]["sec_offset"] == 1.0


def test_build_tape_folds_multi_hop_rows_of_one_tx():
    rows = [_row("0xaaa", "buy", 100, T0, usd=5, amount=10, tx="0xt"), _row("0xaaa", "buy", 100, T0, usd=7, amount=20, tx="0xt")]
    tape = analyze.build_tape(rows, T0, 120)
    assert len(tape["trades"]) == 1 and tape["trades"][0]["usd"] == 12 and tape["trades"][0]["token_amount"] == 30


# ---- cleaning and eligibility ----


def test_same_tx_opposite_legs_collapse_to_the_dominant_side():
    L = Launches()
    rows = [
        _row("0xhook", "buy", 1000, T0 + 1, usd=264.08, tx="0xa"), _row("0xhook", "sell", 1000, T0 + 1, usd=3.24, tx="0xa"),   # hook fee leg on a buy
        _row("0xseller", "sell", 1001, T0 + 2, usd=247.83, tx="0xb"), _row("0xseller", "buy", 1001, T0 + 2, usd=0.59, tx="0xb"),  # fee leg on a sell
        _row("0xrelay", "buy", 1002, T0 + 3, usd=100, tx="0xc"), _row("0xrelay", "sell", 1002, T0 + 3, usd=98, tx="0xc"),       # launchpad router relaying a third-party sell
        _row("0xtwo", "buy", 1003, T0 + 4, usd=50, tx="0xd"), _row("0xtwo", "sell", 1010, T0 + 9, usd=40, tx="0xe"),            # a real round trip: 2 txs
    ]
    L.add("p", T0, rows)
    _, trades = L.prepared()
    kinds = sorted((t["wallet"], t["kind"]) for t in trades)
    assert kinds == [("0xhook", "buy"), ("0xrelay", "buy"), ("0xseller", "sell"), ("0xtwo", "buy"), ("0xtwo", "sell")]


def test_late_opener_tape_is_anchored_on_its_first_trade():
    rows = buys(T0 + 145, ("0xa", 0, 0), ("0xb", 3, 1)) + buys(T0 + 145, ("0xc", 400, 110)) + buys(T0 + 145, ("0xlate", 900, 130))
    by_creation = analyze.build_tape(rows, T0, 120)
    assert by_creation["trades"] == []  # all of it is past created+120s
    tape = analyze.build_tape(rows, T0, 120, anchor="first_trade")
    assert [t["wallet"] for t in tape["trades"]] == ["0xa", "0xb", "0xc"]
    assert tape["launch_ts"] == T0 + 145 and tape["trades"][1]["sec_offset"] == 1.0


def test_fee_leg_does_not_make_a_seller_a_sniper_or_a_buyer_a_round_tripper():
    L = Launches()
    for i in range(3):
        c = T0 + i * 600
        rows = buys(c, ("0xdev", 0, 0))
        rows += [_row("0xhook", "buy", 1005, c + 2, usd=100, tx=f"0xh{i}"), _row("0xhook", "sell", 1005, c + 2, usd=1.4, tx=f"0xh{i}")]
        rows += [_row("0xseller", "sell", 1006, c + 3, usd=250, tx=f"0xs{i}"), _row("0xseller", "buy", 1006, c + 3, usd=0.6, tx=f"0xs{i}")]
        L.add(f"q{i}", c, rows)
    stats = L.stats()
    assert stats["0xhook"]["label"] == "serial_sniper" and stats["0xhook"]["round_trips"] == 0
    assert "0xseller" not in stats


def test_truncated_tapes_and_extra_pools_are_not_launches():
    L = Launches()
    L.add("first", T0, buys(T0, ("0xdev", 0, 0), ("0xs", 3, 1)), token="0xtok")
    L.add("feetier", T0 + 300, buys(T0 + 300, ("0xs", 3, 1)), token="0xtok")         # same token, later pool
    L.add("cut", T0 + 600, buys(T0 + 600, ("0xs", 3, 1)), truncated=1)
    L.add("quiet", T0 + 900, [], status="quiet")
    pools, trades = L.prepared()
    assert [p["pool"] for p in pools] == ["first"]
    assert {t["pool"] for t in trades} == {"first"}


# ---- classes ----


def _classic_dataset():
    """0xdev creates three launches; 0xs1/0xs2 snipe together and hold; 0x4337.. is a bundler;
    0xone snipes once; 0xslow buys 30s in."""
    L = Launches()
    plan = {
        "p1": [("0xdev", 0, 0), ("0xs1", 2, 1), ("0xs2", 2, 1), ("0x4337bund", 1, 1), ("0xslow", 50, 30)],
        "p2": [("0xdev", 0, 0), ("0xs1", 3, 1), ("0xs2", 3, 1), ("0x4337bund", 1, 1)],
        "p3": [("0xdev", 0, 0), ("0xs1", 1, 1), ("0xs2", 5, 2), ("0x4337bund", 2, 1), ("0xone", 2, 1)],
        "p4": [("0xother", 0, 0), ("0xs1", 4, 2), ("0xs2", 4, 2)],
        "p5": [("0xother2", 0, 0), ("0xs1", 3, 2)],
    }
    for i, (pool, spec) in enumerate(plan.items()):
        c = T0 + i * 3600
        rows = buys(c, *spec, block_base=1000 * (i + 1))
        if pool == "p1":
            rows += sells(c, ("0xs1", 20, 12, 30), block_base=1000)  # sold at a profit after 11s
        L.add(pool, c, rows)
    return L


def test_wallet_classes():
    stats = _classic_dataset().stats()
    assert stats["0xs1"]["label"] == "serial_sniper" and stats["0xs1"]["launches"] == 5 and stats["0xs1"]["round_trips"] == 1
    assert stats["0xs2"]["label"] == "serial_sniper" and stats["0xs2"]["launches"] == 4
    assert stats["0xdev"]["label"] == "serial_launcher" and stats["0xdev"]["launcher_launches"] == 3
    assert stats["0x4337bund"]["label"] == "bundler"
    assert stats["0xone"]["label"] == "one_off"
    assert "0xslow" not in stats


def test_thresholds_are_replayable():
    L = _classic_dataset()
    stats = L.stats(SniperConfig(snipe_s=60, min_launches=1))
    assert stats["0xslow"]["label"] == "serial_sniper"


def test_block0_buyer_is_a_sniper_when_the_developer_is_someone_else():
    L = Launches()
    info = {}
    for i in range(3):
        c = T0 + i * 600
        L.add(f"d{i}", c, buys(c, ("0xfast", 0, 0), ("0xdevwallet", 1, 0)))
        info[("robinhood", f"d{i}")] = {"developer": "0xDEVWALLET"}
    stats = L.stats(info=info)
    assert stats["0xfast"]["label"] == "serial_sniper"
    assert stats["0xdevwallet"]["label"] == "serial_launcher"


def test_block0_is_not_a_launch_block_when_trading_opens_later():
    """Without a known developer, block 0 only means 'creator' if it is the pool's creation block."""
    L = Launches()
    for i in range(3):
        c = T0 + i * 600
        L.add(f"late{i}", c, buys(c, ("0xearly", 0, 10), ("0xx", 2, 11)))  # first trade 10s after creation
    stats = L.stats()
    assert stats["0xearly"]["label"] == "serial_sniper"


def test_round_trippers_dust_bots_and_timer_sellers():
    L = Launches()
    for i in range(4):
        c = T0 + i * 600
        rows = buys(c, ("0xdev", 0, 0), ("0xfarm", 9, 1, 8.0), ("0xdust", 9, 1, 0.06), ("0xhold", 12, 2, 50), ("0xtimer", 10, 1, 6.0), ("0xflip", 10, 1, 100))
        rows += sells(c, ("0xfarm", 40, 7, 7.9), ("0xdust", 41, 8, 0.05), ("0xhold", 400, 100, 60), ("0xtimer", 300, 61, 5.8), ("0xflip", 40, 8, 150))
        L.add(f"r{i}", c, rows)
    stats = L.stats()
    assert stats["0xfarm"]["label"] == "round_tripper" and stats["0xfarm"]["round_trips"] == 4
    assert stats["0xdust"]["label"] == "dust_bot"
    assert stats["0xhold"]["label"] == "serial_sniper"
    assert stats["0xtimer"]["label"] == "round_tripper" and stats["0xtimer"]["timer_seller"]
    assert stats["0xflip"]["label"] == "serial_sniper"  # fast, but it makes money


def test_break_even_round_trips_count_as_bots():
    L = Launches()
    for i in range(3):
        c = T0 + i * 600
        rows = buys(c, ("0xdev", 0, 0), ("0xbe", 9, 1, 1.07)) + sells(c, ("0xbe", 40, 9, 1.09))  # +1.9%: inside tolerance
        L.add(f"b{i}", c, rows)
    assert L.stats()["0xbe"]["label"] == "round_tripper"


# ---- packs and outcomes ----


def test_packs_do_not_glue_a_hit_everything_sniper_to_a_subgroup():
    L = Launches()
    for i in range(8):
        c = T0 + i * 600
        spec = [("0xlaunch", 0, 0), ("0xbig", 45, 5)]
        if i < 4:
            spec += [("0xf1", 9, 1), ("0xf2", 9, 1)]
        L.add(f"r{i}", c, buys(c, *spec))
    pools, trades = L.prepared()
    stats = analyze.wallet_stats(trades, pools, CFG)
    packs = analyze.find_packs(trades, stats, CFG)
    farm = next(p for p in packs if "0xf1" in p["members"])
    assert set(farm["members"]) == {"0xf1", "0xf2"} and farm["same_block_rate"] == 1.0
    assert all("0xbig" not in p["members"] and "0xlaunch" not in p["members"] for p in packs)


def test_outcomes_compare_groups():
    L = _classic_dataset()
    cfg = SniperConfig(snipe_s=10, min_launches=3, snapshot_ages_min=[60])
    pools, trades = L.prepared(cfg)
    stats = analyze.wallet_stats(trades, pools, cfg)
    # snipers pay 10 USD / 1000 tokens = 0.01; p1 trades at 0.001 an hour later (down 90%), p5 at 0.02
    created = {p["pool"]: p["created_ts"] for p in pools}
    snaps = [
        # h1 still counts p1's launch trades; the 30-minute counter shows it is dead
        {"chain": "robinhood", "pool": "p1", "target_age_min": 60, "ts": created["p1"] + 3620, "price_usd": 0.001, "trades_h1": 5, "trades_m30": 0, "reserve_usd": 50},
        {"chain": "robinhood", "pool": "p5", "target_age_min": 60, "ts": created["p5"] + 3620, "price_usd": 0.02, "trades_h1": 8, "trades_m30": 3, "reserve_usd": 5000},
        # taken 3 hours late (collector was down): must be ignored
        {"chain": "robinhood", "pool": "p2", "target_age_min": 60, "ts": created["p2"] + 4 * 3600, "price_usd": 0.5, "trades_h1": 1, "trades_m30": 1, "reserve_usd": 1},
    ]
    outcomes = analyze.launch_outcomes(pools, trades, snaps, stats, cfg)
    p1 = next(o for o in outcomes if o["pool"] == "p1")
    assert p1["alive_60m"] is False and abs(p1["multiple_60m"] - 0.1) < 1e-9
    assert "alive_60m" not in next(o for o in outcomes if o["pool"] == "p2")
    cmp = analyze.compare_outcomes(outcomes, 60)
    assert cmp["with"]["n"] == 2 and cmp["with"]["down_90_pct"] == 50.0


def test_daily_windows_cover_every_launch():
    pools = [{"created_ts": T0}, {"created_ts": T0 + 86400}]  # T0 is 14:13 UTC, so the two fall on consecutive days
    wins = analyze.daily_windows(pools)
    assert len(wins) == 2 and wins[0][0] <= T0 < wins[0][1] == wins[1][0] <= T0 + 86400 < wins[1][1]
    assert all(start % 86400 == 0 for start, _ in wins)


def test_parse_pool_strips_network_prefix():
    row = {
        "attributes": {"address": "0xpool", "name": "X / WETH", "pool_created_at": "2026-09-29T17:25:42Z"},
        "relationships": {"base_token": {"data": {"id": "robinhood_0xtok"}}, "quote_token": {"data": {"id": "robinhood_0xweth"}}, "dex": {"data": {"id": "some-dex-v2"}}},
    }
    p = analyze.parse_pool(row)
    assert p["token"] == "0xtok" and p["quote"] == "0xweth" and p["dex"] == "some-dex-v2" and p["created_ts"] > 0


def test_class_cache_roundtrip(tmp_path):
    from sniper.collect import current_serial, refresh_classes
    from sniper.store import Store

    L = _classic_dataset()
    store = Store(tmp_path / "t.db")
    for p in L.pools:
        store.add_pool("robinhood", p)
        store.save_capture("robinhood", p["pool"], {"launch_block": 0, "launch_ts": p["launch_ts"], "trades": [t for t in L.trades if t["pool"] == p["pool"]], "truncated": False})
    store.commit()
    counts = refresh_classes(store, SniperConfig(snipe_s=10, min_launches=3, analysis_days=None))
    live = current_serial(store)
    assert live["0xs1"]["label"] == "serial_sniper" and live["0xdev"]["label"] == "serial_launcher"
    assert "0xone" not in live and counts["serial_sniper"] == 2


def test_snapshot_window_skips_pools_the_collector_missed(tmp_path):
    from sniper.store import Store

    store = Store(tmp_path / "s.db")
    now = T0 + 10 * 3600
    for name, age_h in (("ontime", 1.02), ("late", 7.0)):
        store.add_pool("robinhood", {"pool": name, "token": name, "created_ts": now - age_h * 3600})
        store.set_status("robinhood", name, "captured")
    due = store.due_snapshots("robinhood", now, [60, 360])
    assert due == {60: ["ontime"]}


def test_secondary_pools_are_flagged_at_discovery(tmp_path):
    from sniper.store import Store

    store = Store(tmp_path / "s.db")
    assert store.add_pool("robinhood", {"pool": "a", "token": "0xt", "created_ts": T0}) == "pending"
    assert store.add_pool("robinhood", {"pool": "b", "token": "0xt", "created_ts": T0 + 60}) == "secondary"
    assert store.add_pool("robinhood", {"pool": "c", "token": "0xu", "created_ts": T0 + 60}) == "pending"


def test_daily_credit_counter_persists(tmp_path):
    from sniper.store import Store

    store = Store(tmp_path / "s.db")
    store.add_credits("2026-09-29", 40)
    store.add_credits("2026-09-29", 2)
    store.commit()
    assert Store(tmp_path / "s.db").credits_on("2026-09-29") == 42


def test_trades_window_walks_forward_so_a_page_cap_only_cuts_the_end():
    import asyncio

    from sniper.collect import trades_window

    trades = [{"id": f"t{i}", "attributes": {"tx_hash": f"0x{i}", "ts": 1000 + i}} for i in range(60)]  # one trade per second

    class FakeClient:
        async def get(self, path, params):
            rows = [t for t in trades if params["from"] <= t["attributes"]["ts"] <= params["to"]][::-1]  # newest first
            start = int(params.get("cursor") or 0)
            page = rows[start : start + 5]
            nxt = str(start + 5) if start + 5 < len(rows) else None
            return {"data": page, "meta": {"next_cursor": nxt}}

    rows, complete_until = asyncio.run(trades_window(FakeClient(), "robinhood", "p", 1000, 1059, max_pages=4, slice_s=15))
    got = sorted(r["ts"] for r in rows)
    assert got[0] == 1000 and got == list(range(1000, 1000 + len(got)))  # the earliest trades are always there
    assert complete_until is not None and complete_until <= 1015
    rows, complete_until = asyncio.run(trades_window(FakeClient(), "robinhood", "p", 1000, 1059, max_pages=50, slice_s=15))
    assert len(rows) == 60 and complete_until is None
