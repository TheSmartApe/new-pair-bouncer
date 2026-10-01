"""Pure analysis over recorded launch tapes. No network calls, so every threshold can be replayed offline.

Everything this module outputs (snipe, round trip, serial sniper, round-tripper, dust bot, serial
launcher, pack, alive) is a label this repo computes from CoinGecko API trade and pool data. None of
them are CoinGecko API fields.

Typical use:
    pools, trades = prepare(store.all_pools(), store.all_trades(), cfg)   # eligible launches, cleaned trades
    stats = wallet_stats(trades, pools, cfg, info=store.launch_info())
"""
import statistics
from collections import defaultdict
from datetime import datetime

from .config import SniperConfig, snapshot_tolerance_min

SERIAL_CLASSES = ("serial_sniper", "round_tripper", "dust_bot", "serial_launcher")
BOT_CLASSES = ("round_tripper", "dust_bot")


def _f(x, default=None):
    if x is None or x == "":
        return default
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def iso_ts(value) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _rel_id(pool_row: dict, rel: str) -> str | None:
    """'robinhood_0xabc' -> '0xabc' for a relationship id on a pool row."""
    rid = (((pool_row.get("relationships") or {}).get(rel) or {}).get("data") or {}).get("id")
    if not rid:
        return None
    return rid.split("_", 1)[1] if "_" in rid else rid


def parse_pool(pool_row: dict) -> dict | None:
    """A new_pools API row -> the fields we store. None if it has no creation time."""
    a = pool_row.get("attributes") or {}
    created = iso_ts(a.get("pool_created_at"))
    if not a.get("address") or created is None:
        return None
    return {
        "pool": a["address"],
        "token": _rel_id(pool_row, "base_token"),
        "quote": _rel_id(pool_row, "quote_token"),
        "dex": (((pool_row.get("relationships") or {}).get("dex") or {}).get("data") or {}).get("id"),
        "name": a.get("name"),
        "created_ts": created,
    }


def pool_snapshot(pool_row: dict) -> dict:
    """A pools/multi API row -> outcome fields for a snapshot."""
    a = pool_row.get("attributes") or {}
    txs = a.get("transactions") or {}

    def count(window):
        w = txs.get(window) or {}
        return (w.get("buys") or 0) + (w.get("sells") or 0)

    return {
        "price_usd": _f(a.get("base_token_price_usd")),
        "reserve_usd": _f(a.get("reserve_in_usd")),
        "fdv_usd": _f(a.get("fdv_usd")),
        "trades_h1": count("h1"),
        "trades_m30": count("m30"),
        "trades_m15": count("m15"),
        "trades_m5": count("m5"),
        "volume_h1": _f((a.get("volume_usd") or {}).get("h1")),
        "buys_h24": (((a.get("transactions") or {}).get("h24")) or {}).get("buys") or 0,
    }


def build_tape(rows: list[dict], created_ts: float, window_s: int, truncated: bool = False, anchor: str = "created") -> dict:
    """Raw trades/range rows for one pool -> the launch tape: every trade in the first `window_s`
    seconds after creation (anchor="created") or after the first trade (anchor="first_trade", for
    pools whose trading opened late), with block and second offsets from the pool's first trade.

    Offsets are measured from the first indexed trade, not from the creation timestamp, because the
    creation block usually carries the deployer's own first buy and that is the natural "block 0".
    Rows are stored as-is (fee legs included) so the raw tape can always be re-analyzed;
    `drop_fee_legs` cleans them at analysis time."""
    cut = created_ts + window_s
    if anchor == "first_trade":
        opens = [iso_ts(r.get("block_timestamp")) for r in rows if r.get("kind") in ("buy", "sell")]
        opens = [t for t in opens if t is not None]
        cut = (min(opens) if opens else created_ts) + window_s
    merged: dict[tuple, dict] = {}
    for r in rows:
        kind = r.get("kind")
        if kind not in ("buy", "sell"):
            continue
        ts = iso_ts(r.get("block_timestamp"))
        block = r.get("block_number")
        wallet = (r.get("tx_from_address") or "").lower()
        if ts is None or block is None or not wallet or ts > cut:
            continue
        amount = _f(r.get("to_token_amount") if kind == "buy" else r.get("from_token_amount"), 0.0)
        key = (r.get("tx_hash"), wallet, kind)
        if key in merged:  # one tx can emit several swap rows (multi-hop routes); fold them together
            merged[key]["usd"] += _f(r.get("volume_in_usd"), 0.0)
            merged[key]["token_amount"] += amount
            continue
        merged[key] = {
            "tx_hash": r.get("tx_hash"),
            "wallet": wallet,
            "kind": kind,
            "block": int(block),
            "ts": ts,
            "usd": _f(r.get("volume_in_usd"), 0.0),
            "token_amount": amount,
        }
    trades = sorted(merged.values(), key=lambda t: (t["block"], t["ts"]))
    if not trades:
        return {"launch_block": None, "launch_ts": None, "trades": [], "truncated": truncated}
    launch_block = trades[0]["block"]
    launch_ts = min(t["ts"] for t in trades)
    for t in trades:
        t["block_offset"] = t["block"] - launch_block
        t["sec_offset"] = t["ts"] - launch_ts
    return {"launch_block": launch_block, "launch_ts": launch_ts, "trades": trades, "truncated": truncated}


# ---- cleaning and eligibility ----


def drop_fee_legs(trades: list[dict], cfg: SniperConfig) -> list[dict]:
    """Collapses a sender's opposite-side rows inside ONE transaction to the dominant side.

    Two protocol patterns produce them. Uniswap v4 hook pools (e.g. Bankr) emit a small swap in the
    other direction next to every trade (a $264 buy with a $3 "sell"). Pons-v2 router calls can sell
    tokens pulled from a third-party address and hand the proceeds to the sender, who then shows both
    a buy and a sell. Neither is the sender buying and selling. Left in, every such trade looks like a
    0-second round trip, and a pure seller looks like a sniper. The side with the larger USD value is
    kept (the buy on a tie)."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for t in trades:
        groups[(t["chain"], t["pool"], t["tx_hash"], t["wallet"])].append(t)
    out = []
    for rows in groups.values():
        buys = [r for r in rows if r["kind"] == "buy"]
        sells = [r for r in rows if r["kind"] == "sell"]
        if buys and sells:
            b, s = sum(r["usd"] or 0 for r in buys), sum(r["usd"] or 0 for r in sells)
            rows = buys if b >= s else sells
        out += rows
    return out


def eligible_launches(pools: list[dict]) -> dict[tuple, dict]:
    """Captured, complete launch tapes of each token's FIRST pool.

    Excluded: truncated tapes (the earliest trades are missing, so block 0 would be wrong) and extra
    pools of a token that already had a pool (fee-tier or post-graduation pools are not launches)."""
    first_pool: dict[tuple, tuple] = {}
    for p in sorted(pools, key=lambda p: p["created_ts"]):
        if p.get("token"):
            first_pool.setdefault((p["chain"], p["token"]), (p["chain"], p["pool"]))
    out = {}
    for p in pools:
        if p.get("status") != "captured" or p.get("truncated"):
            continue
        if p.get("token") and first_pool.get((p["chain"], p["token"])) != (p["chain"], p["pool"]):
            continue
        out[(p["chain"], p["pool"])] = p
    return out


def prepare(pools: list[dict], trades: list[dict], cfg: SniperConfig, since: float | None = None, until: float | None = None) -> tuple[list[dict], list[dict]]:
    """(eligible launches, their cleaned trades), optionally limited to launches created in [since, until)."""
    elig = eligible_launches(pools)
    if since is not None or until is not None:
        elig = {k: p for k, p in elig.items() if (since is None or p["created_ts"] >= since) and (until is None or p["created_ts"] < until)}
    trades = [t for t in trades if (t["chain"], t["pool"]) in elig]
    return list(elig.values()), drop_fee_legs(trades, cfg)


# ---- per-launch positions and wallet classes ----


def is_bundler(wallet: str, cfg: SniperConfig) -> bool:
    w = wallet.lower()
    return any(w.startswith(p.lower()) for p in cfg.bundler_prefixes)


def snipe_rows(trades: list[dict], cfg: SniperConfig) -> list[dict]:
    """Buys inside the snipe window."""
    return [t for t in trades if t["kind"] == "buy" and t["sec_offset"] is not None and t["sec_offset"] <= cfg.snipe_s]


def observation_hours(pools: list[dict]) -> float:
    created = [p["created_ts"] for p in pools]
    if len(created) < 2:
        return 0.0
    return (max(created) - min(created)) / 3600


def _developer_of(info: dict | None, key: tuple) -> str | None:
    dev = ((info or {}).get(key) or {}).get("developer")
    return dev.lower() if dev else None


def launch_positions(trades: list[dict], cfg: SniperConfig, pools: list[dict] | None = None, info: dict | None = None) -> dict[tuple, dict]:
    """(chain, pool, wallet) -> what that wallet did in that launch's tape, for every sniping wallet.

    `launcher` marks the wallet as the launch's creator: it is the token's developer_address when
    token info knows it; otherwise it bought in block 0 AND block 0 is the pool's creation block
    (first trade within `creation_block_s` of pool creation). A wallet that buys in block 0 of a pool
    whose developer is someone else is a sniper, not a launcher."""
    meta = {(p["chain"], p["pool"]): p for p in (pools or [])}
    buys: dict[tuple, list[dict]] = defaultdict(list)
    sells: dict[tuple, list[dict]] = defaultdict(list)
    for t in trades:
        key = (t["chain"], t["pool"], t["wallet"])
        (buys if t["kind"] == "buy" else sells)[key].append(t)
    out = {}
    for key, bs in buys.items():
        snipes = [b for b in bs if b["sec_offset"] <= cfg.snipe_s]
        if not snipes:
            continue
        launch = (key[0], key[1])
        block0 = any(b["block_offset"] == 0 for b in snipes)
        dev = _developer_of(info, launch)
        if dev:
            launcher = key[2] == dev
        else:
            p = meta.get(launch) or {}
            at_creation = p.get("launch_ts") is not None and p.get("created_ts") is not None and p["launch_ts"] - p["created_ts"] <= cfg.creation_block_s
            launcher = block0 and at_creation
        first_buy = min(b["ts"] for b in snipes)
        ss = sorted(sells.get(key, []), key=lambda s: s["ts"])
        later = [s for s in ss if s["ts"] >= first_buy]
        hold = (later[0]["ts"] - first_buy) if later else None
        out[key] = {
            "snipes": snipes,
            "block0": block0,
            "launcher": launcher,
            "blocks": {b["block"] for b in snipes},
            "sold": bool(later),
            "round_trip": hold is not None and hold <= cfg.roundtrip_max_s,
            "hold_s": hold,
            "buy_usd": sum(b["usd"] for b in bs),
            "sell_usd": sum(s["usd"] for s in ss),
        }
    return out


def _not_profitable(positions: list[dict], cfg: SniperConfig) -> bool:
    """Net loss or break-even (within `breakeven_tolerance` of what was bought) across positions."""
    bought = sum(p["buy_usd"] for p in positions)
    net = sum(p["sell_usd"] - p["buy_usd"] for p in positions)
    return net <= cfg.breakeven_tolerance * bought


def wallet_stats(trades: list[dict], pools: list[dict], cfg: SniperConfig, info: dict | None = None) -> dict[str, dict]:
    """Per-wallet snipe record and a code-derived class for every wallet that sniped at least once.
    Pass the output of `prepare()`: eligible launches and cleaned trades.

    Classes, first match wins:
      bundler          ERC-4337 bundler address (submits other people's trades)
      one_off          fewer than `min_launches` sniped launches
      serial_launcher  the launch's creator in most of its launches (see launch_positions)
      dust_bot         median snipe below `dust_usd`
      round_tripper    sells within `roundtrip_max_s` in >= `roundtrip_min_rate` of its launches, or sells
                       on a fixed timer, and does not make money doing it: paying to be in the buyer list
      serial_sniper    everything else that snipes >= `min_launches` launches
    """
    positions = launch_positions(trades, cfg, pools, info)
    hours = observation_hours(pools)
    by_wallet: dict[str, list[tuple]] = defaultdict(list)
    for (chain, pool, wallet), pos in positions.items():
        by_wallet[wallet].append((chain, pool, pos))

    out = {}
    for wallet, items in by_wallet.items():
        n = len(items)
        poss = [pos for _, _, pos in items]
        snipes = [s for pos in poss for s in pos["snipes"]]
        trips = [p for p in poss if p["round_trip"]]
        sold = [p for p in poss if p["sold"]]
        holds = [p["hold_s"] for p in sold]
        median_hold = statistics.median(holds) if holds else None
        timer = (
            len(sold) >= cfg.min_launches
            and len(sold) / n >= cfg.roundtrip_min_rate
            and sum(1 for h in holds if abs(h - median_hold) <= cfg.timer_tolerance_s) / len(holds) >= 0.8
        )
        rt_rate = len(trips) / n
        launcher_n = sum(1 for p in poss if p["launcher"])
        median_snipe = statistics.median(s["usd"] for s in snipes)
        rate = n / hours if hours >= 0.25 else None
        if is_bundler(wallet, cfg):
            label = "bundler"
        elif rate is not None and cfg.max_launches_per_hour is not None and rate > cfg.max_launches_per_hour:
            label = "infrastructure"
        elif n < cfg.min_launches:
            label = "one_off"
        elif launcher_n / n >= 0.5:
            label = "serial_launcher"
        elif median_snipe < cfg.dust_usd:
            label = "dust_bot"
        elif (rt_rate >= cfg.roundtrip_min_rate and _not_profitable(trips, cfg)) or (timer and _not_profitable(sold, cfg)):
            label = "round_tripper"
        else:
            label = "serial_sniper"
        out[wallet] = {
            "wallet": wallet,
            "label": label,
            "launches": n,
            "launcher_launches": launcher_n,
            "block0_launches": sum(1 for p in poss if p["block0"]),
            "round_trips": len(trips),
            "round_trip_rate": round(rt_rate, 3),
            "round_trip_cost_usd": round(sum(p["buy_usd"] - p["sell_usd"] for p in trips), 2),
            "timer_seller": timer,
            "sold_in_window": len(sold),
            "median_hold_in_window_s": round(median_hold, 1) if median_hold is not None else None,
            "median_sec_offset": round(statistics.median(s["sec_offset"] for s in snipes), 1),
            "median_block_offset": statistics.median(s["block_offset"] for s in snipes),
            "snipe_usd": round(sum(s["usd"] for s in snipes), 2),
            "median_snipe_usd": round(median_snipe, 2),
            "chains": sorted({c for c, _, _ in items}),
            "first_ts": min(s["ts"] for s in snipes),
            "last_ts": max(s["ts"] for s in snipes),
            "launches_per_hour": round(rate, 2) if rate is not None else None,
            "pools": sorted(f"{c}:{p}" for c, p, _ in items),
        }
    return out


def wallets_with(stats: dict[str, dict], *labels: str) -> set[str]:
    return {w for w, s in stats.items() if s["label"] in labels}


def serial_wallets(stats: dict[str, dict]) -> set[str]:
    return wallets_with(stats, *SERIAL_CLASSES)


def find_packs(trades: list[dict], stats: dict[str, dict], cfg: SniperConfig) -> list[dict]:
    """Groups of serial wallets that keep sniping the same launches together, at the same moment.

    Two wallets are linked only when all three hold:
    - they share at least `pack_min_shared` launches;
    - those shared launches are at least `pack_min_overlap` of the UNION of their launches (Jaccard),
      so a sniper that hits every launch is not glued to a smaller group that is a subset of it;
    - in the shared launches, their buys are a median of at most `pack_max_block_gap` blocks apart.
    Linked wallets are merged into packs (union-find). `same_block_rate` is the share of the pack's
    shared launches in which two or more members bought in the very same block.

    Launchers are left out: their link to a launch is creating it, not sniping it. The report ties
    them to round-trippers separately, through developer_address."""
    serial = wallets_with(stats, "serial_sniper", "round_tripper", "dust_bot")
    positions = launch_positions(trades, cfg)
    per_launch: dict[tuple, dict[str, set[int]]] = defaultdict(dict)
    for (chain, pool, wallet), pos in positions.items():
        if wallet in serial:
            per_launch[(chain, pool)][wallet] = pos["blocks"]

    shared: dict[tuple[str, str], list[int]] = defaultdict(list)
    for members in per_launch.values():
        ws = sorted(members)
        for i in range(len(ws)):
            for j in range(i + 1, len(ws)):
                a, b = members[ws[i]], members[ws[j]]
                shared[(ws[i], ws[j])].append(min(abs(x - y) for x in a for y in b))

    parent = {w: w for w in serial}

    def find(w):
        while parent[w] != w:
            parent[w] = parent[parent[w]]
            w = parent[w]
        return w

    for (a, b), gaps in shared.items():
        n = len(gaps)
        union = stats[a]["launches"] + stats[b]["launches"] - n
        if n >= cfg.pack_min_shared and n / union >= cfg.pack_min_overlap and statistics.median(gaps) <= cfg.pack_max_block_gap:
            parent[find(a)] = find(b)

    groups: dict[str, set[str]] = defaultdict(set)
    for w in serial:
        groups[find(w)].add(w)

    packs = []
    for members in groups.values():
        if len(members) < 2:
            continue
        together = []
        same_block = 0
        for launch, present in per_launch.items():
            inside = {w: blocks for w, blocks in present.items() if w in members}
            if len(inside) < 2:
                continue
            together.append(launch)
            block_counts: dict[int, int] = defaultdict(int)
            for blocks in inside.values():
                for b in blocks:
                    block_counts[b] += 1
            if any(c >= 2 for c in block_counts.values()):
                same_block += 1
        if not together:
            continue
        labels = defaultdict(int)
        for w in members:
            labels[stats[w]["label"]] += 1
        packs.append(
            {
                "members": sorted(members, key=lambda w: -stats[w]["launches"]),
                "size": len(members),
                "kind": max(labels, key=labels.get),
                "shared_launches": len(together),
                "same_block_rate": round(same_block / len(together), 3),
                "snipe_usd": round(sum(stats[w]["snipe_usd"] for w in members), 2),
                "round_trip_cost_usd": round(sum(stats[w]["round_trip_cost_usd"] for w in members), 2),
                "chains": sorted({c for c, _ in together}),
                "launches": sorted(f"{c}:{p}" for c, p in together),
            }
        )
    return sorted(packs, key=lambda p: (-p["shared_launches"], -p["size"]))


def is_alive(snap: dict, age_min: int, cfg: SniperConfig) -> bool | None:
    """Did the pool trade recently at snapshot time? None when the snapshot can't tell.

    Up to +2h the API's h1 counter still includes the launch trades themselves (it is bucketed, not
    a precise rolling hour), so a pool that died after its first minute reads as active. Early ages
    use the 30-minute counter instead, which cannot contain the launch."""
    if age_min <= 120:
        m30 = snap.get("trades_m30")
        return None if m30 is None else m30 >= cfg.alive_min_trades_h1
    return (snap.get("trades_h1") or 0) >= cfg.alive_min_trades_h1


def launch_outcomes(pools: list[dict], trades: list[dict], snapshots: list[dict], stats: dict[str, dict], cfg: SniperConfig, buyer_counts: dict | None = None) -> list[dict]:
    """Per eligible launch: who sniped it and what the pool looked like at each snapshot age.

    `snipe_vwap` is the average price paid inside the snipe window by everyone except bots,
    bundlers and launchers: the reference for `multiple_<age>m` = snapshot price / snipe_vwap.
    Snapshots taken more than snapshot_tolerance_min(age) late are ignored. `buyer_counts`
    ((chain, pool) -> distinct buyers in the tape) is used when `trades` holds only snipers' rows."""
    snipers = wallets_with(stats, "serial_sniper")
    bots = wallets_with(stats, *BOT_CLASSES)
    trippers = wallets_with(stats, "round_tripper")
    skip = bots | wallets_with(stats, "bundler", "serial_launcher")
    by_launch: dict[tuple, list[dict]] = defaultdict(list)
    for t in trades:
        by_launch[(t["chain"], t["pool"])].append(t)
    snaps: dict[tuple, dict[int, dict]] = defaultdict(dict)
    for s in snapshots:
        snaps[(s["chain"], s["pool"])][s["target_age_min"]] = s

    out = []
    for p in pools:
        key = (p["chain"], p["pool"])
        rows = by_launch.get(key, [])
        snipes = snipe_rows(rows, cfg)
        priced = [s for s in snipes if s["wallet"] not in skip] or snipes
        qty = sum(s["token_amount"] for s in priced)
        vwap = (sum(s["usd"] for s in priced) / qty) if qty else None
        buyers = {t["wallet"] for t in rows if t["kind"] == "buy"}
        n_buyers = max(len(buyers), (buyer_counts or {}).get(key, 0))
        rec = {
            "chain": p["chain"],
            "pool": p["pool"],
            "name": p.get("name"),
            "dex": p.get("dex"),
            "token": p.get("token"),
            "created_ts": p["created_ts"],
            "buyers_window": n_buyers,
            "real_buyers_window": n_buyers - len(buyers & skip),
            "bot_share": round(len(buyers & bots) / n_buyers, 3) if n_buyers else 0.0,
            "round_tripper_share": round(len(buyers & trippers) / n_buyers, 3) if n_buyers else 0.0,
            "snipers": len({s["wallet"] for s in snipes}),
            "serial_snipers": sorted({s["wallet"] for s in snipes if s["wallet"] in snipers}),
            "round_trippers": sorted(buyers & trippers),
            "snipe_usd": round(sum(s["usd"] for s in snipes), 2),
            "snipe_vwap": vwap,
        }
        for age in cfg.snapshot_ages_min:
            s = snaps.get(key, {}).get(age)
            if not s or (s["ts"] - p["created_ts"]) / 60 - age > snapshot_tolerance_min(age):
                continue
            alive = is_alive(s, age, cfg)
            if alive is None:
                continue
            rec[f"alive_{age}m"] = alive
            rec[f"multiple_{age}m"] = (s["price_usd"] / vwap) if (vwap and s.get("price_usd") is not None) else None
            rec[f"reserve_{age}m"] = s.get("reserve_usd")
        out.append(rec)
    return out


def summarize_group(rows: list[dict], age: int) -> dict:
    rows = [r for r in rows if f"alive_{age}m" in r]
    if not rows:
        return {"n": 0}
    mults = [r[f"multiple_{age}m"] for r in rows if r.get(f"multiple_{age}m") is not None]
    return {
        "n": len(rows),
        "alive_pct": round(100 * sum(1 for r in rows if r[f"alive_{age}m"]) / len(rows), 1),
        "median_multiple": round(statistics.median(mults), 3) if mults else None,
        "below_entry_pct": round(100 * sum(1 for m in mults if m < 1) / len(mults), 1) if mults else None,
        "down_90_pct": round(100 * sum(1 for m in mults if m <= 0.1) / len(mults), 1) if mults else None,
    }


def compare_outcomes(outcomes: list[dict], age: int, field: str = "serial_snipers") -> dict:
    """Launches where `field` (serial_snipers or round_trippers) is non-empty vs launches where it is empty."""
    with_ = [r for r in outcomes if r[field]]
    without = [r for r in outcomes if not r[field] and r["snipers"]]
    return {"age_min": age, "field": field, "with": summarize_group(with_, age), "without": summarize_group(without, age)}


def sniper_track_record(wallet: str, outcomes: list[dict], age: int) -> dict:
    """How the launches a given wallet sniped looked `age` minutes later."""
    rows = [r for r in outcomes if (wallet in r["serial_snipers"] or wallet in r["round_trippers"]) and f"alive_{age}m" in r]
    mults = [r[f"multiple_{age}m"] for r in rows if r.get(f"multiple_{age}m") is not None]
    return {
        "launches_with_outcome": len(rows),
        "alive_pct": round(100 * sum(1 for r in rows if r[f"alive_{age}m"]) / len(rows), 1) if rows else None,
        "median_multiple": round(statistics.median(mults), 3) if mults else None,
    }


def daily_windows(pools: list[dict], day_s: int = 86400) -> list[tuple[float, float]]:
    """Consecutive UTC-day windows [start, end) covering the launches, oldest first."""
    if not pools:
        return []
    first = min(p["created_ts"] for p in pools)
    last = max(p["created_ts"] for p in pools)
    start = first - (first % day_s)
    out = []
    while start <= last:
        out.append((start, start + day_s))
        start += day_s
    return out
