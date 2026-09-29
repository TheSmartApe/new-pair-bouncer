"""SQLite store: launches, their launch-window trades, outcome snapshots and wallet profiles."""
import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS pools (
    chain TEXT NOT NULL,
    pool TEXT NOT NULL,
    token TEXT,
    name TEXT,
    dex TEXT,
    quote TEXT,
    created_ts REAL NOT NULL,
    seen_ts REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | captured | quiet | empty | expired | error
    captured_ts REAL,
    launch_block INTEGER,
    launch_ts REAL,
    n_trades INTEGER,
    truncated INTEGER DEFAULT 0,
    note TEXT,
    PRIMARY KEY (chain, pool)
);
CREATE TABLE IF NOT EXISTS trades (
    chain TEXT NOT NULL,
    pool TEXT NOT NULL,
    tx_hash TEXT NOT NULL,
    wallet TEXT NOT NULL,
    kind TEXT NOT NULL,
    block INTEGER,
    ts REAL,
    block_offset INTEGER,
    sec_offset REAL,
    usd REAL,
    token_amount REAL,
    PRIMARY KEY (chain, pool, tx_hash, wallet, kind)
);
CREATE INDEX IF NOT EXISTS trades_wallet ON trades(wallet);
CREATE INDEX IF NOT EXISTS trades_cpw ON trades(chain, pool, wallet, kind);
CREATE TABLE IF NOT EXISTS launch_info (
    chain TEXT NOT NULL,
    pool TEXT NOT NULL,
    token TEXT,
    developer TEXT,
    developer_holding_pct REAL,
    gt_score REAL,
    holders INTEGER,
    launchpad_json TEXT,
    fetched_ts REAL,
    PRIMARY KEY (chain, pool)
);
CREATE TABLE IF NOT EXISTS snapshots (
    chain TEXT NOT NULL,
    pool TEXT NOT NULL,
    target_age_min INTEGER NOT NULL,
    ts REAL NOT NULL,
    price_usd REAL,
    reserve_usd REAL,
    fdv_usd REAL,
    trades_h1 INTEGER,
    volume_h1 REAL,
    PRIMARY KEY (chain, pool, target_age_min)
);
CREATE TABLE IF NOT EXISTS wallets (
    wallet TEXT PRIMARY KEY,
    profiled_ts REAL,
    profile_json TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
    ts REAL NOT NULL,
    chain TEXT NOT NULL,
    pool TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")  # reports can read while the 24/7 collector writes
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    # ---- pools ----

    def known_pools(self, chain: str) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT pool FROM pools WHERE chain=?", (chain,))}

    def add_pool(self, chain: str, pool: dict):
        self.db.execute(
            "INSERT OR IGNORE INTO pools (chain, pool, token, name, dex, quote, created_ts, seen_ts) VALUES (?,?,?,?,?,?,?,?)",
            (chain, pool["pool"], pool.get("token"), pool.get("name"), pool.get("dex"), pool.get("quote"), pool["created_ts"], time.time()),
        )

    def pending(self, chain: str, created_before: float) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM pools WHERE chain=? AND status='pending' AND created_ts<=? ORDER BY created_ts",
            (chain, created_before),
        ).fetchall()

    def expire_stale(self, chain: str, created_before: float) -> int:
        cur = self.db.execute(
            "UPDATE pools SET status='expired' WHERE chain=? AND status='pending' AND created_ts<?",
            (chain, created_before),
        )
        return cur.rowcount

    def set_status(self, chain: str, pool: str, status: str, note: str | None = None):
        self.db.execute("UPDATE pools SET status=?, note=?, captured_ts=? WHERE chain=? AND pool=?", (status, note, time.time(), chain, pool))

    def save_capture(self, chain: str, pool: str, tape: dict):
        self.db.execute(
            "UPDATE pools SET status='captured', captured_ts=?, launch_block=?, launch_ts=?, n_trades=?, truncated=? WHERE chain=? AND pool=?",
            (time.time(), tape["launch_block"], tape["launch_ts"], len(tape["trades"]), int(tape["truncated"]), chain, pool),
        )
        self.db.executemany(
            "INSERT OR IGNORE INTO trades (chain, pool, tx_hash, wallet, kind, block, ts, block_offset, sec_offset, usd, token_amount) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                (chain, pool, t["tx_hash"], t["wallet"], t["kind"], t["block"], t["ts"], t["block_offset"], t["sec_offset"], t["usd"], t["token_amount"])
                for t in tape["trades"]
            ],
        )

    # ---- snapshots ----

    def due_snapshots(self, chain: str, now: float, ages_min: list[int]) -> dict[int, list[str]]:
        due: dict[int, list[str]] = {}
        for age in ages_min:
            rows = self.db.execute(
                """SELECT p.pool FROM pools p WHERE p.chain=? AND p.status='captured' AND p.created_ts<=?
                   AND NOT EXISTS (SELECT 1 FROM snapshots s WHERE s.chain=p.chain AND s.pool=p.pool AND s.target_age_min=?)""",
                (chain, now - age * 60, age),
            ).fetchall()
            if rows:
                due[age] = [r[0] for r in rows]
        return due

    def save_snapshot(self, chain: str, pool: str, age_min: int, snap: dict):
        self.db.execute(
            "INSERT OR REPLACE INTO snapshots (chain, pool, target_age_min, ts, price_usd, reserve_usd, fdv_usd, trades_h1, volume_h1) VALUES (?,?,?,?,?,?,?,?,?)",
            (chain, pool, age_min, time.time(), snap.get("price_usd"), snap.get("reserve_usd"), snap.get("fdv_usd"), snap.get("trades_h1"), snap.get("volume_h1")),
        )

    # ---- wallets + alerts ----

    def save_profile(self, wallet: str, profile: dict):
        self.db.execute("INSERT OR REPLACE INTO wallets (wallet, profiled_ts, profile_json) VALUES (?,?,?)", (wallet, time.time(), json.dumps(profile)))

    def profiles(self) -> dict[str, dict]:
        return {r["wallet"]: json.loads(r["profile_json"]) for r in self.db.execute("SELECT * FROM wallets")}

    def save_alert(self, chain: str, pool: str, payload: dict):
        self.db.execute("INSERT INTO alerts (ts, chain, pool, payload_json) VALUES (?,?,?,?)", (time.time(), chain, pool, json.dumps(payload)))

    def save_launch_info(self, chain: str, pool: str, info: dict):
        self.db.execute(
            "INSERT OR REPLACE INTO launch_info (chain, pool, token, developer, developer_holding_pct, gt_score, holders, launchpad_json, fetched_ts) VALUES (?,?,?,?,?,?,?,?,?)",
            (chain, pool, info.get("token"), info.get("developer"), info.get("developer_holding_pct"), info.get("gt_score"), info.get("holders"), json.dumps(info.get("launchpad")), time.time()),
        )

    def launch_info(self) -> dict[tuple, dict]:
        return {(r["chain"], r["pool"]): dict(r) for r in self.db.execute("SELECT * FROM launch_info")}

    def live_serial(self, snipe_s: int, roundtrip_max_s: int, min_launches: int) -> dict[str, dict]:
        """Serial wallets computed in SQL, so the 24/7 loop stays fast as the database grows.
        Mirrors analyze.wallet_stats' classes (minus the optional infrastructure rate cap)."""
        rows = self.db.execute(
            """
            WITH s AS (
                SELECT chain, pool, wallet, MIN(ts) AS buy_ts, MAX(block_offset = 0) AS b0
                FROM trades WHERE kind = 'buy' AND sec_offset <= ? GROUP BY chain, pool, wallet
            ), a AS (
                SELECT t.chain, t.pool, t.wallet,
                       SUM(CASE WHEN t.kind = 'buy' THEN t.usd ELSE 0 END) AS b_usd,
                       SUM(CASE WHEN t.kind = 'sell' THEN t.usd ELSE 0 END) AS s_usd
                FROM trades t JOIN s ON t.chain = s.chain AND t.pool = s.pool AND t.wallet = s.wallet
                GROUP BY t.chain, t.pool, t.wallet
            ), x AS (
                SELECT s.*, a.b_usd, a.s_usd,
                       (SELECT MIN(t.ts) FROM trades t
                        WHERE t.chain = s.chain AND t.pool = s.pool AND t.wallet = s.wallet
                          AND t.kind = 'sell' AND t.ts >= s.buy_ts) AS sell_ts
                FROM s JOIN a ON a.chain = s.chain AND a.pool = s.pool AND a.wallet = s.wallet
            ), y AS (
                SELECT x.*, (sell_ts IS NOT NULL AND sell_ts - buy_ts <= ?) AS trip FROM x
            )
            SELECT wallet, COUNT(*) AS launches, SUM(b0) AS b0, SUM(trip) AS trips,
                   ROUND(SUM(CASE WHEN trip THEN b_usd - s_usd ELSE 0 END), 2) AS trip_cost
            FROM y GROUP BY wallet HAVING COUNT(*) >= ?
            """,
            (snipe_s, roundtrip_max_s, min_launches),
        ).fetchall()
        return {r["wallet"]: dict(r) for r in rows}

    # ---- reads for analysis ----

    def all_trades(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM trades")]

    def all_pools(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM pools")]

    def all_snapshots(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM snapshots")]

    def commit(self):
        self.db.commit()
