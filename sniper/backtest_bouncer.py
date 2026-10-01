"""Walk-forward replay of the bouncer's free (stage 0) checks over every recorded launch.

For each launch, in creation order, the bot's memory (known bots, entity clusters, dev launch counts)
is rebuilt only from launches created BEFORE it, in steps of `step_min` minutes, so no check sees the
future. Each paper trade is replayed like the live bot would have done it:
  decision  at pool creation + window_s + lag_s, when the live collector captures and checks a launch
  entry     the last minute-candle close before the decision (CoinGecko pool OHLCV), flat slippage
  exit      the first candle that hits the take-profit or the stop-loss, else the time limit, priced at
            the +60m snapshot through that snapshot's liquidity (constant-product impact), plus fees
Three books: control (every launch), crowd (the crowd rule alone) and bouncer (ENTER). Minute candles
are fetched once per pool (1 API call) and cached in the database.

Stage 1 and 2 checks (token info, wallet profiles) are not replayed: their historical values weren't
recorded at decision time.
"""
import statistics
from collections import defaultdict

from . import analyze, bouncer, checks
from .checks import Memory
from .store import is_rug
from .config import SniperConfig


async def fetch_candles(store, launches: list[dict], cfg: SniperConfig) -> int:
    """Minute candles for launches that have a +60m outcome and no cached candles. 1 call each."""
    import asyncio

    from core.client import CoinGeckoClient, CoinGeckoError

    import time

    b = cfg.bouncer_config()
    fetched = store.candles_fetched() if hasattr(store, "candles_fetched") else {}
    now = time.time()

    def complete_by(p):  # the last candle the replay can use: the decision plus the hold limit, plus a margin
        return p["created_ts"] + cfg.window_s + cfg.lag_s + b.max_hold_min * 60 + 300

    todo = [p for p in launches if now >= complete_by(p) and fetched.get((p["chain"], p["pool"]), 0) < complete_by(p)]
    if not todo:
        return 0
    got = 0
    async with CoinGeckoClient() as client:

        async def one(p):
            nonlocal got
            try:
                d = await client.get(f"/onchain/networks/{p['chain']}/pools/{p['pool']}/ohlcv/minute",
                                     {"aggregate": 1, "limit": 90, "currency": "usd", "token": "base", "before_timestamp": int(p["created_ts"]) + 85 * 60})
            except CoinGeckoError:
                return
            store.save_candles(p["chain"], p["pool"], d.get("data", {}).get("attributes", {}).get("ohlcv_list", []))
            got += 1

        await asyncio.gather(*(one(p) for p in todo))
    store.commit()
    return got


def _summary(rows: list[dict], position_usd: float = 100) -> dict:
    with_outcome = [r for r in rows if r["dead"] is not None]
    mults = [r["multiple"] for r in with_outcome if r["multiple"] is not None]
    pnl = [r["paper_pnl"] for r in with_outcome if r["paper_pnl"] is not None]
    return {
        "launches": len(rows),
        "with_outcome": len(with_outcome),
        "with_pnl": len(pnl),
        "dead_pct": round(100 * sum(1 for r in with_outcome if r["dead"]) / len(with_outcome), 1) if with_outcome else None,
        "median_multiple": round(statistics.median(mults), 3) if mults else None,
        "down_50_pct": round(100 * sum(1 for m in mults if m <= 0.5) / len(mults), 1) if mults else None,
        "down_90_pct": round(100 * sum(1 for m in mults if m <= 0.1) / len(mults), 1) if mults else None,
        "up_2x_pct": round(100 * sum(1 for m in mults if m >= 2) / len(mults), 1) if mults else None,
        "paper_pnl_usd": round(sum(pnl), 2) if pnl else None,
        "paper_return_pct": round(100 * sum(pnl) / (len(pnl) * position_usd), 2) if pnl else None,
    }


def run(store, cfg: SniperConfig, step_min: int = 15, fetch: bool = False) -> dict:
    b = cfg.bouncer_config()
    pools_all = store.all_pools()
    elig = analyze.eligible_launches(pools_all)
    trades = analyze.drop_fee_legs([t for t in store.all_trades() if (t["chain"], t["pool"]) in elig], cfg)  # cleaned
    raw_trades = [t for t in store.all_trades() if (t["chain"], t["pool"]) in elig]
    by_pool: dict[tuple, list[dict]] = defaultdict(list)
    raw_by_pool: dict[tuple, list[dict]] = defaultdict(list)
    for t in trades:
        by_pool[(t["chain"], t["pool"])].append(t)
    for t in raw_trades:
        raw_by_pool[(t["chain"], t["pool"])].append(t)
    snaps = {(s["chain"], s["pool"]): s for s in store.all_snapshots() if s["target_age_min"] == 60}
    info = store.launch_info()
    supplies = store.supplies() if hasattr(store, "supplies") else {}
    deployer_of = store.deployers(cfg.creation_block_s) if hasattr(store, "deployers") else {}
    launches = sorted(elig.values(), key=lambda p: p["created_ts"])
    if not launches:
        return {"launches": 0}
    if fetch:
        import asyncio

        asyncio.run(fetch_candles(store, [p for p in launches if (p["chain"], p["pool"]) in snaps], cfg))
    candles = store.candles() if hasattr(store, "candles") else {}

    rows: list[dict] = []
    memory = Memory()
    step_start = None
    for p in launches:
        key = (p["chain"], p["pool"])
        if step_start is None or p["created_ts"] >= step_start + step_min * 60:
            step_start = p["created_ts"]
            prior = [q for q in launches if q["created_ts"] < step_start]
            prior_keys = {(q["chain"], q["pool"]) for q in prior}
            prior_trades = [t for t in trades if (t["chain"], t["pool"]) in prior_keys]
            prior_info = {k: v for k, v in info.items() if k in prior_keys}
            stats = analyze.wallet_stats(prior_trades, prior, cfg, info=prior_info) if prior else {}
            packs = analyze.find_packs(prior_trades, stats, cfg) if prior else []
            pack_of = {}
            for i, pk in enumerate(packs):
                for w in pk["members"]:
                    pack_of[w] = {"pack_id": f"P{i + 1}", "size": pk["size"], "shared_launches": pk["shared_launches"]}
            dev_counts: dict[str, int] = defaultdict(int)
            dev_rugs: dict[str, int] = defaultdict(int)
            for q in prior:
                dep = deployer_of.get((q["chain"], q["pool"]))
                if not dep:
                    continue
                dev_counts[dep] += 1
                s = snaps.get((q["chain"], q["pool"]))
                if s and s["ts"] < step_start and is_rug(s.get("reserve_usd"), s.get("trades_m30"), b.rug_reserve_usd):  # only outcomes already known
                    dev_rugs[dep] += 1
            memory = Memory(classes={w: {"label": s["label"], "launches": s["launches"]} for w, s in stats.items()}, packs=pack_of,
                            dev_launches=dict(dev_counts), dev_rugs=dict(dev_rugs))

        tape = by_pool.get(key, [])
        developer = (info.get(key) or {}).get("developer")  # like the live bot: token info, else the shared stand-in rule
        f = checks.tape_features(tape, b, developer, supplies.get((p["chain"], (p.get("token") or "").lower())),
                                 raw_trades=raw_by_pool.get(key, []), created_ts=p["created_ts"], creation_block_s=cfg.creation_block_s)
        found = checks.stage0(f, memory, b, reserve_usd=None)
        v = checks.decide(found, 0, f)
        snap = snaps.get(key)
        dead = multiple = paper_pnl = exit_reason = None
        if snap and (snap["ts"] - p["created_ts"]) / 60 - 60 <= 6:
            alive = analyze.is_alive(snap, 60, cfg)
            if alive is not None:
                dead = not alive
                decision_ts = max(p["created_ts"] + cfg.window_s, max((t["ts"] for t in tape), default=0)) + cfg.lag_s
                exit_reserve = checks.usable_reserve(snap.get("reserve_usd"), snap.get("trades_m30"), b)
                trade = bouncer.simulate_path(candles.get(key, []), decision_ts, b, exit_reserve, snap.get("price_usd")) if key in candles else None
                if trade:
                    paper_pnl, exit_reason = trade["pnl"], trade["reason"]
                    if snap.get("price_usd") is not None:
                        multiple = snap["price_usd"] / trade["entry_raw"]
        values = {c.key: c.value for c in found if isinstance(c.value, (int, float, bool))}
        rows.append({
            "pool": p["pool"], "name": p.get("name"), "dex": p.get("dex"), "created_ts": p["created_ts"], "verdict": v.verdict,
            "fails": [c.key for c in v.fails], "warns": [c.key for c in v.warns], "values": values,
            "trades": f["trades"], "volume_usd": f["volume_usd"], "cohort": len(f["cohort"]), "crowd": checks.crowd_only(f, b),
            "dead": dead, "multiple": multiple, "paper_pnl": paper_pnl, "exit_reason": exit_reason,
        })

    by_verdict = {name: _summary([r for r in rows if r["verdict"] == name], b.position_usd) for name in ("ENTER", "WATCH", "AVOID")}
    fail_counts: dict[str, int] = defaultdict(int)
    for r in rows:
        for k in r["fails"]:
            fail_counts[k] += 1
    worst = [r for r in rows if r["multiple"] is not None and r["multiple"] <= 0.1]
    good = [r for r in rows if r["multiple"] is not None and r["multiple"] >= 2]
    return {
        "launches": len(rows),
        "hours": round((launches[-1]["created_ts"] - launches[0]["created_ts"]) / 3600, 2),
        "control": _summary(rows, b.position_usd),
        "crowd": _summary([r for r in rows if r["crowd"]], b.position_usd),
        "bouncer": _summary([r for r in rows if r["verdict"] == "ENTER"], b.position_usd),
        "by_verdict": by_verdict,
        "fail_reasons": dict(sorted(fail_counts.items(), key=lambda kv: -kv[1])),
        "worst_avoided": f"{sum(1 for r in worst if r['verdict'] != 'ENTER')} of {len(worst)} launches that lost 90%+ in an hour were not ENTER",
        "good_rejected": f"{sum(1 for r in good if r['verdict'] != 'ENTER')} of {len(good)} launches that doubled in an hour were not ENTER",
        "rows": rows,
    }


def fmt(res: dict) -> str:
    if not res.get("launches"):
        return "no recorded launches yet"
    lines = [f"{res['launches']} launches over {res['hours']}h, walk-forward, stage 0 checks, entry at decision time, simulated TP/SL/time exits"]
    for name in ("control", "crowd", "bouncer"):
        s = res[name]
        lines.append(f"{name:8} {s['launches']:5} launches | {s['with_outcome']} with outcome | dead {s['dead_pct']}% | median x{s['median_multiple']} | "
                     f"-90%: {s['down_90_pct']}% | 2x+: {s['up_2x_pct']}% | paper ${s['paper_pnl_usd']} ({s['paper_return_pct']}%)")
    for name, s in res["by_verdict"].items():
        lines.append(f"  {name:6} {s['launches']:5} | dead {s['dead_pct']}% | median x{s['median_multiple']} | -90%: {s['down_90_pct']}% | paper {s['paper_return_pct']}%")
    lines.append(f"fail reasons: {res['fail_reasons']}")
    lines.append(res["worst_avoided"])
    lines.append(res["good_rejected"])
    return "\n".join(lines)
