"""The 24/7 loop: discover new pools, record each launch's first minutes, snapshot outcomes, raise alerts.

Per sweep and per chain:
1. page /new_pools until a page is entirely already-known (every Nth sweep, deeper, for pools indexed
   late). Pools of a token that already had a pool are stored as 'secondary' and never captured;
2. for pools whose launch window has closed (+ indexing lag), batch-check activity with /pools/multi.
   Pools with too few buys are re-checked each sweep until `quiet_recheck_min`, then marked quiet;
3. pull /trades/range for the launch window in ascending time slices, store the tape, and alert if
   wallets already classed as serial were in the first seconds;
4. snapshot captured pools at each `snapshot_ages_min` age via /pools/multi (30 pools per call).
Every hour, in a worker thread: refresh wallet classes, rewrite the report, list launches that need
token info; then fetch that info. Credits are counted per UTC day in the database.
"""
import asyncio
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from core.client import CoinGeckoClient, CoinGeckoError, CreditBudgetExceeded

from . import analyze
from .config import REPORTS_DIR, SniperConfig
from .store import Store


def _log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _utc_day(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), tz=timezone.utc).strftime("%Y-%m-%d")


async def pools_multi(client: CoinGeckoClient, chain: str, addresses: list[str]) -> list[dict]:
    """Batch pool lookup (30 per call), uncached: these responses are never reused."""
    out = []
    for i in range(0, len(addresses), 30):
        d = await client.get(f"/onchain/networks/{chain}/pools/multi/{','.join(addresses[i : i + 30])}", {"include": "base_token"})
        out += d.get("data", [])
    return out


async def token_supplies(client: CoinGeckoClient, chain: str, tokens: list[str], batch: int = 20) -> dict[str, float]:
    """token -> normalized total supply via tokens/multi. A batch that times out is split in smaller
    ones; a single token that still fails is left out (asked again next time)."""
    out: dict[str, float] = {}

    async def fetch(group: list[str]):
        try:
            d = await client.get(f"/onchain/networks/{chain}/tokens/multi/{','.join(group)}")
        except CreditBudgetExceeded:
            raise
        except CoinGeckoError:
            if len(group) == 1:
                return
            half = len(group) // 2
            await fetch(group[:half])
            await fetch(group[half:])
            return
        for row in d.get("data", []):
            a = row.get("attributes") or {}
            s = analyze._f(a.get("normalized_total_supply"))
            if a.get("address") and s:
                out[a["address"].lower()] = s

    for i in range(0, len(tokens), batch):
        await fetch(tokens[i : i + batch])
    return out


async def backfill_supply(client: CoinGeckoClient, store: Store, limit: int = 3000) -> int:
    """Total supply for captured launches that don't have it yet (used by the backtest)."""
    by_chain: dict[str, list[str]] = {}
    for chain, token in store.tokens_missing_supply(limit):
        by_chain.setdefault(chain, []).append(token)
    n = 0
    for chain, tokens in by_chain.items():
        got = await token_supplies(client, chain, tokens)
        for token in tokens:
            store.save_supply(chain, token, got.get(token))  # None marks "asked, unknown" so it isn't re-asked forever
            n += int(token in got)
    store.commit()
    return n


async def discover(client: CoinGeckoClient, store: Store, chain: str, cfg: SniperConfig, deep: bool = False) -> dict:
    """Pages new_pools (newest first). A normal sweep stops at the first page with nothing new. A deep
    sweep keeps going until pools are older than `deep_discovery_min`, because pools can be indexed
    several minutes late and then sit below already-known pools in the listing."""
    known = store.known_pools(chain)
    counts = {"new": 0, "secondary": 0}
    horizon = time.time() - cfg.deep_discovery_min * 60
    for page in range(1, cfg.max_new_pool_pages + 1):
        d = await client.get(f"/onchain/networks/{chain}/new_pools", {"include": "base_token,quote_token,dex", "page": page})
        rows = d.get("data", [])
        if not rows:
            break
        parsed = [p for p in (analyze.parse_pool(r) for r in rows) if p]
        fresh = 0
        for p in sorted(parsed, key=lambda p: p["created_ts"]):  # oldest first, so a token's first pool is inserted first
            if p["pool"] not in known:
                status = store.add_pool(chain, p)
                known.add(p["pool"])
                fresh += 1
                counts["secondary" if status == "secondary" else "new"] += 1
        oldest = min((p["created_ts"] for p in parsed), default=None)
        if deep:
            if oldest is not None and oldest < horizon:
                break
        elif fresh == 0:
            break
    store.commit()
    return counts


async def trades_window(client: CoinGeckoClient, chain: str, pool: str, from_ts: int, to_ts: int, max_pages: int, slice_s: int = 15) -> tuple[list[dict], float | None]:
    """Every trade in [from_ts, to_ts], fetched in ascending `slice_s`-second slices.

    Each trades/range response is newest first, so paging one big window and running out of pages
    would lose the EARLIEST trades, which are the launch itself. Walking forward in small slices means
    a page cap can only cut the end of the window. Returns (rows, complete_until_ts): None when the
    whole window was fetched, else the timestamp up to which the tape is complete."""
    rows: list[dict] = []
    seen: set = set()
    pages = 0
    lo = from_ts
    while lo < to_ts:
        hi = min(lo + slice_s, to_ts)  # the API wants from < to; slices share their boundary second, deduplicated by row id
        cursor = None
        slice_start = len(rows)
        while True:
            if pages >= max_pages:
                return rows[:slice_start], float(lo)  # drop the partial slice: the tape is exact up to `lo`
            params = {"from": lo, "to": hi}
            if cursor:
                params["cursor"] = cursor
            d = await client.get(f"/onchain/networks/{chain}/pools/{pool}/trades/range", params)
            pages += 1
            page = d.get("data", [])
            for r in page:
                rid = r.get("id") or (r.get("attributes") or {}).get("tx_hash")
                if rid not in seen:
                    seen.add(rid)
                    rows.append(r.get("attributes", r))
            cursor = (d.get("meta") or {}).get("next_cursor")
            if not cursor or not page:
                break
        lo = hi
    return rows, None


def _tape(rows, complete_until, created, cfg, anchor="created") -> dict:
    tape = analyze.build_tape(rows, created, cfg.window_s, complete_until is not None, anchor=anchor)
    tape["complete_until_ts"] = complete_until
    return tape


async def capture_tape(client: CoinGeckoClient, chain: str, p, cfg: SniperConfig, max_pages: int | None = None) -> dict:
    """The launch tape for one pool, anchored on its first trade.

    Most launches trade within a second of pool creation, so the window [created-15, created+window_s]
    holds the whole opening. When it is empty or its first trade comes more than `late_open_s` after
    creation, trading opened later: the pool's earliest minute candle gives the opening minute, and the
    tape is re-fetched from there so late openers get the same full window."""
    pages = max_pages or cfg.max_trade_pages
    created = p["created_ts"]
    rows, complete_until = await trades_window(client, chain, p["pool"], int(created) - 15, int(created) + cfg.window_s, pages)
    tape = _tape(rows, complete_until, created, cfg)
    if tape["trades"] and tape["launch_ts"] - created <= cfg.late_open_s:
        return tape
    d = await client.get(f"/onchain/networks/{chain}/pools/{p['pool']}/ohlcv/minute", {"aggregate": 1, "limit": 1000, "currency": "usd", "token": "base"})
    candles = d.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
    if not candles:
        return tape
    first_minute = int(min(c[0] for c in candles))
    if first_minute + 60 < created:
        return tape
    if first_minute + 60 + cfg.window_s + cfg.lag_s > time.time():
        return {**tape, "not_ready": True}
    rows, complete_until = await trades_window(client, chain, p["pool"], first_minute - 15, first_minute + 60 + cfg.window_s, pages)
    late = _tape(rows, complete_until, created, cfg, anchor="first_trade")
    if late["trades"]:
        late["reanchored"] = True
        return late
    return tape


async def capture_ready(client: CoinGeckoClient, store: Store, chain: str, cfg: SniperConfig, serial: dict[str, dict], bouncer=None) -> dict:
    now = time.time()
    expired = store.expire_stale(chain, now - cfg.max_pending_age_min * 60)
    ready = store.pending(chain, now - cfg.window_s - cfg.lag_s)
    counts = {"ready": len(ready), "captured": 0, "quiet": 0, "waiting": 0, "reanchored": 0, "truncated": 0, "empty": 0, "retry": 0, "expired": expired, "alerts": 0,
              "ENTER": 0, "WATCH": 0, "AVOID": 0}
    if not ready:
        return counts

    activity: dict[str, dict] = {}
    try:
        for row in await pools_multi(client, chain, [p["pool"] for p in ready]):
            addr = (row.get("attributes") or {}).get("address")
            if addr:
                activity[addr] = analyze.pool_snapshot(row)
    except CreditBudgetExceeded:
        raise
    except CoinGeckoError as exc:
        _log(f"{chain}: pools/multi failed ({exc}); capturing without the activity pre-filter")

    if bouncer is not None:
        bouncer.refresh_memory()  # before any capture of this sweep, so the memory never includes the launch being judged
    supplies = store.supplies()
    missing = sorted({(p["token"] or "").lower() for p in ready if p["token"] and (chain, (p["token"] or "").lower()) not in supplies})
    try:
        for token, supply in (await token_supplies(client, chain, missing)).items():
            store.save_supply(chain, token, supply)
            supplies[(chain, token)] = supply
    except CreditBudgetExceeded:
        raise
    except CoinGeckoError as exc:
        _log(f"{chain}: tokens/multi failed ({exc}); supply checks skipped this sweep")

    async def one(p):
        snap = activity.get(p["pool"])
        act = {**(snap or {}), "supply": supplies.get((chain, (p["token"] or "").lower()))}
        age_min = (now - p["created_ts"]) / 60
        if snap is not None and snap["buys_h24"] < cfg.min_buys:
            if age_min < cfg.quiet_recheck_min:
                counts["waiting"] += 1  # trading can open minutes after pool creation: look again next sweep
                return
            store.set_status(chain, p["pool"], "quiet", f"{snap['buys_h24']} buys after {age_min:.0f} min")
            counts["quiet"] += 1
            return
        try:
            tape = await capture_tape(client, chain, p, cfg)
        except CreditBudgetExceeded:
            raise
        except CoinGeckoError as exc:
            store.set_note(chain, p["pool"], f"retrying: {str(exc)[:160]}")  # stays pending; expire_stale bounds the retries
            counts["retry"] += 1
            return
        if tape.get("not_ready"):
            counts["waiting"] += 1  # a late opener whose re-anchored window hasn't been indexed yet
            return
        if not tape["trades"]:
            if age_min < cfg.quiet_recheck_min:
                counts["waiting"] += 1
                return
            store.set_status(chain, p["pool"], "empty", "no trades found")
            counts["empty"] += 1
            return
        counts["reanchored"] += int(bool(tape.get("reanchored")))
        counts["truncated"] += int(tape["truncated"])
        store.save_capture(chain, p["pool"], tape)
        counts["captured"] += 1
        if tape["truncated"]:
            return  # no alerts or verdicts from a tape whose end was cut off
        if bouncer is not None:
            try:
                v = await bouncer.evaluate(client, chain, dict(p), tape, act)
                counts[v.verdict] += 1
                if v.verdict == "ENTER":
                    _log(f"ENTER {chain} {p['name']}: passed {len(v.checks)} checks (stage {v.stage}) at ${v.features.get('price_usd') or 0:.8g}")
            except CreditBudgetExceeded:
                raise
            except CoinGeckoError as exc:
                _log(f"{chain} {p['name']}: checks failed ({str(exc)[:120]}), no verdict")
        hits = {}
        clean = analyze.drop_fee_legs([{**t, "chain": chain, "pool": p["pool"]} for t in tape["trades"]], cfg)
        for t in sorted(clean, key=lambda t: t["block"]):
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
            rows = await pools_multi(client, chain, pools)
        except CreditBudgetExceeded:
            raise
        except CoinGeckoError as exc:
            _log(f"{chain}: snapshot +{age}m failed ({exc}), retrying next sweep")
            continue
        seen = set()
        for row in rows:
            addr = (row.get("attributes") or {}).get("address")
            if addr:
                store.save_snapshot(chain, addr, age, analyze.pool_snapshot(row))
                seen.add(addr)
                n += 1
        for pool in set(pools) - seen:  # the API dropped the pool: record it as dead rather than retrying forever
            store.save_snapshot(chain, pool, age, {"price_usd": None, "reserve_usd": None, "trades_h1": 0, "trades_m30": 0, "trades_m15": 0})
    store.commit()
    return n


def refresh_classes(store: Store, cfg: SniperConfig, data: dict | None = None) -> dict[str, int]:
    """Recomputes every wallet's class and caches the serial ones for the live alerts."""
    data = data or store.load_for_analysis(cfg.snipe_s, since=_since(cfg))
    pools, trades = analyze.prepare(data["pools"], data["trades"], cfg, since=data.get("since") or None)
    stats = analyze.wallet_stats(trades, pools, cfg, info=data["info"])
    store.save_classes(stats, analyze.SERIAL_CLASSES)
    store.commit()
    counts: dict[str, int] = {}
    for s in stats.values():
        if s["label"] in analyze.SERIAL_CLASSES:
            counts[s["label"]] = counts.get(s["label"], 0) + 1
    return counts


def current_serial(store: Store) -> dict[str, dict]:
    """Serial wallets as of the last refresh_classes()."""
    return store.load_classes()


def _since(cfg: SniperConfig) -> float | None:
    return time.time() - cfg.analysis_days * 86400 if cfg.analysis_days else None


def analysis_job(db_path, cfg: SniperConfig) -> dict:
    """The hourly CPU-heavy part, run in a worker thread with its own database connection: load once,
    refresh wallet classes, rewrite the report, and list launches that still need token info."""
    from . import report

    store = Store(Path(db_path))
    try:
        data = store.load_for_analysis(cfg.snipe_s, since=_since(cfg))
        counts = refresh_classes(store, cfg, data)
        pools, trades = analyze.prepare(data["pools"], data["trades"], cfg, since=data.get("since") or None)
        stats = analyze.wallet_stats(trades, pools, cfg, info=data["info"])
        store.save_packs(analyze.find_packs(trades, stats, cfg))  # the bouncer's memory of entity clusters
        store.commit()
        serial = set(store.load_classes())
        touched = {(t["chain"], t["pool"]) for t in trades if t["wallet"] in serial}
        todo = [p for p in pools if (p["chain"], p["pool"]) in touched and (p["chain"], p["pool"]) not in data["info"]][:1000]
        text, summary = report.build(store, cfg, data=data)
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        (REPORTS_DIR / "serial-snipers.md").write_text(text, encoding="utf-8")
        return {"classes": counts, "todo": todo, "summary": summary}
    finally:
        store.close()


async def recapture_truncated(client: CoinGeckoClient, store: Store, cfg: SniperConfig) -> tuple[int, int]:
    """One more attempt, with twice the page budget, for tapes that hit the page cap."""
    fixed = still = 0
    for p in store.truncated_pools(cfg.recapture_per_hour):
        try:
            tape = await capture_tape(client, p["chain"], p, cfg, max_pages=2 * cfg.max_trade_pages)
        except CreditBudgetExceeded:
            raise
        except CoinGeckoError:
            store.mark_recaptured(p["chain"], p["pool"])
            still += 1
            continue
        if tape["trades"]:
            store.replace_capture(p["chain"], p["pool"], tape)
        else:
            store.mark_recaptured(p["chain"], p["pool"])
        if tape["truncated"]:
            still += 1
        else:
            fixed += 1
    store.commit()
    return fixed, still


async def housekeeping(client: CoinGeckoClient, store: Store, cfg: SniperConfig, db_path) -> str:
    """Hourly: re-capture truncated tapes, refresh classes and the report off the event loop, then
    fetch developer/launchpad info for launches that drew serial wallets."""
    from .profile import enrich_launches

    fixed, still = await recapture_truncated(client, store, cfg)
    supplied = await backfill_supply(client, store)
    job = await asyncio.to_thread(analysis_job, db_path, cfg)
    results, credits = await enrich_launches(job["todo"], client=client)
    for r in results:
        if "error" not in r:
            store.save_launch_info(r["chain"], r["pool"], r)
    store.commit()
    return f"recaptured {fixed} truncated tapes ({still} still cut), supply for {supplied} tokens, classes {job['classes']}, enriched {len(results)} launches ({credits} credits), report rewritten"


async def run(cfg: SniperConfig, db_path, minutes: float | None = None, max_credits: int | None = None):
    """Sweeps until `minutes` elapse (or forever). Any unexpected error in a sweep is logged with its
    traceback and the loop carries on at the next sweep; the database is the only state, so a
    restart resumes exactly where it stopped. Credits are counted per UTC day in the database; past
    `max_credits_per_day` the collector pauses until the next UTC day."""
    store = Store(db_path)
    deadline = time.time() + minutes * 60 if minutes else None
    bouncer = None
    if cfg.bouncer_enabled:
        from .bouncer import Bouncer

        bouncer = Bouncer(store, cfg)
    async with CoinGeckoClient() as client:
        if max_credits:
            client.max_credits = max_credits
        _log(f"collecting {', '.join(cfg.chains)} every {cfg.interval_s}s -> {db_path}")
        last_housekeeping = time.time()
        sweep = 0
        paused_day = None
        try:
            _log(f"wallet classes refreshed: {await asyncio.to_thread(lambda: analysis_job(db_path, cfg)['classes'])}")
        except Exception:  # noqa: BLE001
            _log("initial class refresh failed, alerts start empty:\n" + traceback.format_exc())
        while True:
            started = time.time()
            day = _utc_day()
            if cfg.max_credits_per_day and store.credits_on(day) >= cfg.max_credits_per_day:
                if paused_day != day:
                    _log(f"daily credit cap of {cfg.max_credits_per_day:,} reached for {day}, pausing until the next UTC day")
                    paused_day = day
                await asyncio.sleep(cfg.interval_s)
                continue
            sweep += 1
            deep = cfg.deep_discovery_every > 0 and sweep % cfg.deep_discovery_every == 0
            before = client.credits_used
            serial = current_serial(store)
            for chain in cfg.chains:
                try:
                    found = await discover(client, store, chain, cfg, deep=deep)
                    c = await capture_ready(client, store, chain, cfg, serial, bouncer)
                    snaps = await take_snapshots(client, store, chain, cfg)
                    book = await bouncer.manage_positions(client, chain, pools_multi) if bouncer else {"open": 0, "closed": 0}
                except CreditBudgetExceeded:
                    store.add_credits(day, client.credits_used - before)
                    store.commit()
                    _log(f"credit budget of {client.max_credits:g} reached, stopping")
                    store.close()
                    return "budget"
                except CoinGeckoError as exc:
                    _log(f"{chain}: sweep failed ({exc}), retrying next sweep")
                    continue
                except Exception:  # noqa: BLE001
                    _log(f"{chain}: unexpected error, retrying next sweep:\n" + traceback.format_exc())
                    continue
                by_label: dict[str, int] = {}
                for v in serial.values():
                    by_label[v["label"]] = by_label.get(v["label"], 0) + 1
                _log(
                    f"{chain}: +{found['new']} new, +{found['secondary']} secondary{' (deep)' if deep else ''} | captured {c['captured']} "
                    f"(late open {c['reanchored']}, cut {c['truncated']}) waiting {c['waiting']} quiet {c['quiet']} empty {c['empty']} retry {c['retry']} "
                    f"expired {c['expired']} | verdicts enter {c['ENTER']} watch {c['WATCH']} avoid {c['AVOID']} | paper open {book['open']} closed {book['closed']} | "
                    f"alerts {c['alerts']} | snapshots {snaps} | serial wallets {by_label} | "
                    f"credits today {store.credits_on(day) + client.credits_used - before:,.0f} | sweep {time.time() - started:.0f}s"
                )
            if time.time() - last_housekeeping >= cfg.housekeeping_every_min * 60:
                last_housekeeping = time.time()
                try:
                    _log(await housekeeping(client, store, cfg, db_path))
                except CreditBudgetExceeded:
                    pass
                except Exception:  # noqa: BLE001
                    _log("housekeeping failed, collection continues:\n" + traceback.format_exc())
            store.add_credits(day, client.credits_used - before)
            store.commit()
            if deadline and time.time() >= deadline:
                break
            wait = cfg.interval_s - (time.time() - started)
            if deadline:
                wait = min(wait, deadline - time.time())
            await asyncio.sleep(max(5, wait))
    store.close()
