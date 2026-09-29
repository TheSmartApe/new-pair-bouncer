"""The 24/7 loop: discover new pools, record each launch's first minutes, snapshot outcomes, raise alerts.

Per sweep and per chain:
1. page through /new_pools until a page is entirely already-known (new launches only);
2. for pools whose launch window has closed (+ indexing lag), batch-check activity with
   /pools/multi and skip pools with fewer than `min_buys` buys;
3. pull /trades/range for the launch window, store the tape, and alert if wallets already known as
   serial snipers were in the first seconds;
4. snapshot captured pools at each `snapshot_ages_min` age via /pools/multi (30 pools per call).
"""
import asyncio
import time
import traceback

from core.client import CoinGeckoClient, CoinGeckoError, CreditBudgetExceeded

from . import analyze
from .config import SniperConfig
from .store import Store


def _log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


async def discover(client: CoinGeckoClient, store: Store, chain: str, cfg: SniperConfig) -> int:
    known = store.known_pools(chain)
    added = 0
    for page in range(1, cfg.max_new_pool_pages + 1):
        d = await client.get(f"/onchain/networks/{chain}/new_pools", {"include": "base_token,quote_token,dex", "page": page})
        rows = d.get("data", [])
        if not rows:
            break
        fresh = 0
        for row in rows:
            parsed = analyze.parse_pool(row)
            if parsed and parsed["pool"] not in known:
                store.add_pool(chain, parsed)
                known.add(parsed["pool"])
                fresh += 1
        added += fresh
        if fresh == 0:
            break
    store.commit()
    return added


async def trades_window(client: CoinGeckoClient, chain: str, pool: str, from_ts: int, to_ts: int, max_pages: int) -> tuple[list[dict], bool]:
    """Every trade in [from_ts, to_ts], cursor-paginated. Pages come newest first, so if a cursor is
    still left after `max_pages`, the missing trades are the EARLIEST ones and the tape is flagged."""
    rows: list[dict] = []
    cursor = None
    for _ in range(max_pages):
        params = {"from": from_ts, "to": to_ts}
        if cursor:
            params["cursor"] = cursor
        d = await client.get(f"/onchain/networks/{chain}/pools/{pool}/trades/range", params)
        page = d.get("data", [])
        rows += [r.get("attributes", r) for r in page]
        cursor = (d.get("meta") or {}).get("next_cursor")
        if not cursor or not page:
            return rows, False
    return rows, True


async def capture_ready(client: CoinGeckoClient, store: Store, chain: str, cfg: SniperConfig, serial: dict[str, dict]) -> dict:
    now = time.time()
    expired = store.expire_stale(chain, now - cfg.max_pending_age_min * 60)
    ready = store.pending(chain, now - cfg.window_s - cfg.lag_s)
    counts = {"ready": len(ready), "captured": 0, "quiet": 0, "empty": 0, "error": 0, "expired": expired, "alerts": 0}
    if not ready:
        return counts

    activity: dict[str, dict] = {}
    try:
        for row in await client.pools_multi(chain, [p["pool"] for p in ready]):
            addr = (row.get("attributes") or {}).get("address")
            if addr:
                activity[addr] = analyze.pool_snapshot(row)
    except CoinGeckoError as exc:
        _log(f"{chain}: pools/multi failed ({exc}); capturing without the activity pre-filter")

    async def one(p):
        act = activity.get(p["pool"])
        if act is not None and act["buys_h24"] < cfg.min_buys:
            store.set_status(chain, p["pool"], "quiet", f"{act['buys_h24']} buys")
            counts["quiet"] += 1
            return
        start = int(p["created_ts"]) - 15
        try:
            rows, truncated = await trades_window(client, chain, p["pool"], start, int(p["created_ts"]) + cfg.window_s, cfg.max_trade_pages)
        except CoinGeckoError as exc:
            store.set_status(chain, p["pool"], "error", str(exc)[:200])
            counts["error"] += 1
            return
        tape = analyze.build_tape(rows, p["created_ts"], cfg.window_s, truncated)
        if not tape["trades"]:
            store.set_status(chain, p["pool"], "empty", "no trades in launch window")
            counts["empty"] += 1
            return
        store.save_capture(chain, p["pool"], tape)
        counts["captured"] += 1
        hits = {}
        for t in tape["trades"]:
            if t["kind"] == "buy" and t["sec_offset"] <= cfg.snipe_s and t["wallet"] in serial and t["wallet"] not in hits:
                hits[t["wallet"]] = t
        if hits:
            counts["alerts"] += 1
            payload = {
                "name": p["name"],
                "snipers": [
                    {"wallet": w, "block_offset": t["block_offset"], "sec_offset": t["sec_offset"], "usd": round(t["usd"], 2),
                     "prior_launches": serial[w]["launches"], "label": serial[w]["label"]}
                    for w, t in sorted(hits.items(), key=lambda kv: kv[1]["block"])
                ],
            }
            store.save_alert(chain, p["pool"], payload)
            who = ", ".join(f"{s['wallet'][:10]}.. {s['label']} (+{s['block_offset']} blk, {s['prior_launches']} prior)" for s in payload["snipers"][:4])
            _log(f"ALERT {chain} {p['name']}: {len(hits)} serial wallet(s) in the first {cfg.snipe_s}s -> {who}")

    await asyncio.gather(*(one(p) for p in ready))
    store.commit()
    return counts


async def take_snapshots(client: CoinGeckoClient, store: Store, chain: str, cfg: SniperConfig) -> int:
    due = store.due_snapshots(chain, time.time(), cfg.snapshot_ages_min)
    n = 0
    for age, pools in due.items():
        try:
            rows = await client.pools_multi(chain, pools)
        except CoinGeckoError as exc:
            _log(f"{chain}: snapshot +{age}m failed ({exc})")
            continue
        seen = set()
        for row in rows:
            addr = (row.get("attributes") or {}).get("address")
            if addr:
                store.save_snapshot(chain, addr, age, analyze.pool_snapshot(row))
                seen.add(addr)
                n += 1
        for pool in set(pools) - seen:  # the API dropped the pool: record it as dead rather than retrying forever
            store.save_snapshot(chain, pool, age, {"price_usd": None, "reserve_usd": None, "trades_h1": 0})
    store.commit()
    return n


def current_serial(store: Store, cfg: SniperConfig) -> dict[str, dict]:
    """Serial wallets known so far, with the same classes analyze.wallet_stats assigns."""
    out = {}
    for w, r in store.live_serial(cfg.snipe_s, cfg.roundtrip_max_s, cfg.min_launches).items():
        n = r["launches"]
        if analyze.is_bundler(w, cfg):
            continue
        if r["b0"] / n >= 0.5:
            label = "serial_launcher"
        elif r["trips"] / n >= cfg.roundtrip_min_rate and r["trip_cost"] >= 0:
            label = "round_tripper"
        else:
            label = "serial_sniper"
        out[w] = {"launches": n, "label": label}
    return out


async def housekeeping(client: CoinGeckoClient, store: Store, cfg: SniperConfig) -> str:
    """Hourly: fetch developer/launchpad info for new launches that drew serial wallets, then rewrite
    reports/serial-snipers.md so the latest numbers are always on disk."""
    from . import report
    from .config import REPORTS_DIR
    from .profile import enrich_launches

    pools, trades = store.all_pools(), store.all_trades()
    stats = analyze.wallet_stats(trades, pools, cfg)
    outcomes = analyze.launch_outcomes(pools, trades, store.all_snapshots(), stats, cfg)
    done = store.launch_info()
    todo = [o for o in outcomes if (o["round_trippers"] or o["serial_snipers"]) and (o["chain"], o["pool"]) not in done][:1000]
    results, credits = await enrich_launches(todo, client=client)
    for r in results:
        if "error" not in r:
            store.save_launch_info(r["chain"], r["pool"], r)
    store.commit()
    text, summary = report.build(store, cfg)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    (REPORTS_DIR / "serial-snipers.md").write_text(text, encoding="utf-8")
    return f"enriched {len(results)} launches ({credits} credits), report rewritten: {summary}"


async def run(cfg: SniperConfig, db_path, minutes: float | None = None, max_credits: int | None = None):
    """Sweeps until `minutes` elapse (or forever). Any unexpected error in a sweep is logged with its
    traceback and the loop carries on at the next sweep; the database is the only state, so a
    restart resumes exactly where it stopped."""
    store = Store(db_path)
    deadline = time.time() + minutes * 60 if minutes else None
    async with CoinGeckoClient() as client:
        if max_credits:
            client.max_credits = max_credits
        _log(f"collecting {', '.join(cfg.chains)} every {cfg.interval_s}s -> {db_path}")
        last_housekeeping = time.time()
        while True:
            started = time.time()
            try:
                serial = current_serial(store, cfg)
            except Exception:  # noqa: BLE001 - a stats hiccup must not stop collection
                _log("serial-wallet refresh failed, alerts paused this sweep:\n" + traceback.format_exc())
                serial = {}
            for chain in cfg.chains:
                try:
                    added = await discover(client, store, chain, cfg)
                    c = await capture_ready(client, store, chain, cfg, serial)
                    snaps = await take_snapshots(client, store, chain, cfg)
                except CreditBudgetExceeded:
                    _log(f"credit budget of {client.max_credits:g} reached, stopping")
                    store.close()
                    return "budget"
                except CoinGeckoError as exc:
                    _log(f"{chain}: sweep failed ({exc}), retrying next sweep")
                    continue
                except Exception:  # noqa: BLE001
                    _log(f"{chain}: unexpected error, retrying next sweep:\n" + traceback.format_exc())
                    continue
                by_label = {}
                for v in serial.values():
                    by_label[v["label"]] = by_label.get(v["label"], 0) + 1
                _log(
                    f"{chain}: +{added} new | captured {c['captured']} quiet {c['quiet']} empty {c['empty']} err {c['error']} "
                    f"expired {c['expired']} | alerts {c['alerts']} | snapshots {snaps} | serial wallets {by_label} | "
                    f"credits {client.credits_used} | sweep {time.time() - started:.0f}s"
                )
            if time.time() - last_housekeeping >= cfg.housekeeping_every_min * 60:
                last_housekeeping = time.time()
                try:
                    _log(await housekeeping(client, store, cfg))
                except Exception:  # noqa: BLE001
                    _log("housekeeping failed, collection continues:\n" + traceback.format_exc())
            if deadline and time.time() >= deadline:
                break
            wait = cfg.interval_s - (time.time() - started)
            if deadline:
                wait = min(wait, deadline - time.time())
            await asyncio.sleep(max(5, wait))
    store.close()
