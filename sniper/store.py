"""SQLite store: launches, their launch-window trades, outcome snapshots, wallet classes and profiles."""
import json
import sqlite3
import time
from pathlib import Path

from .config import snapshot_tolerance_min

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
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | captured | secondary | quiet | empty | expired | error
    captured_ts REAL,
    launch_block INTEGER,
    launch_ts REAL,
    n_trades INTEGER,
    truncated INTEGER DEFAULT 0,
    note TEXT,
    PRIMARY KEY (chain, pool)
);
CREATE INDEX IF NOT EXISTS pools_token ON pools(chain, token);
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
CREATE TABLE IF NOT EXISTS wallet_classes (
    wallet TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    launches INTEGER NOT NULL,
    updated_ts REAL NOT NULL
);
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
CREATE TABLE IF NOT EXISTS money (
    computed_ts REAL PRIMARY KEY,
    summary_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v REAL NOT NULL
);
"""

# Columns added after the first release; applied to existing databases on open.
MIGRATIONS = (
    "ALTER TABLE pools ADD COLUMN recaptures INTEGER DEFAULT 0",
    "ALTER TABLE pools ADD COLUMN complete_until_ts REAL",
    "ALTER TABLE snapshots ADD COLUMN trades_m30 INTEGER",
    "ALTER TABLE snapshots ADD COLUMN trades_m15 INTEGER",
)

TRADE_COLUMNS = "t.chain, t.pool, t.tx_hash, t.wallet, t.kind, t.block, t.ts, t.block_offset, t.sec_offset, t.usd, t.token_amount"


class Store:
    def __init__(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=60)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")  # reports can read while the 24/7 collector writes
        self.db.executescript(SCHEMA)
        for sql in MIGRATIONS:
            try:
                self.db.execute(sql)
            except sqlite3.OperationalError:  # column already there
                pass
        self.db.commit()

    def close(self):
        self.db.close()

    # ---- pools ----

    def known_pools(self, chain: str) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT pool FROM pools WHERE chain=?", (chain,))}

    def add_pool(self, chain: str, pool: dict) -> str:
        """Inserts a newly discovered pool. A pool whose token already has an earlier pool is stored as
        'secondary' (a fee tier or post-graduation pool, not a launch) and is never captured."""
        status = "pending"
        if pool.get("token"):
            older = self.db.execute(
                "SELECT 1 FROM pools WHERE chain=? AND token=? AND pool<>? AND created_ts<=? LIMIT 1",
                (chain, pool["token"], pool["pool"], pool["created_ts"]),
            ).fetchone()
            if older:
                status = "secondary"
        self.db.execute(
            "INSERT OR IGNORE INTO pools (chain, pool, token, name, dex, quote, created_ts, seen_ts, status) VALUES (?,?,?,?,?,?,?,?,?)",
            (chain, pool["pool"], pool.get("token"), pool.get("name"), pool.get("dex"), pool.get("quote"), pool["created_ts"], time.time(), status),
        )
        return status

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

    def set_note(self, chain: str, pool: str, note: str):
        self.db.execute("UPDATE pools SET note=? WHERE chain=? AND pool=?", (note, chain, pool))

    def save_capture(self, chain: str, pool: str, tape: dict):
        self.db.execute(
            "UPDATE pools SET status='captured', captured_ts=?, launch_block=?, launch_ts=?, n_trades=?, truncated=?, complete_until_ts=? WHERE chain=? AND pool=?",
            (time.time(), tape["launch_block"], tape["launch_ts"], len(tape["trades"]), int(tape["truncated"]), tape.get("complete_until_ts"), chain, pool),
        )
        self.db.executemany(
            "INSERT OR IGNORE INTO trades (chain, pool, tx_hash, wallet, kind, block, ts, block_offset, sec_offset, usd, token_amount) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                (chain, pool, t["tx_hash"], t["wallet"], t["kind"], t["block"], t["ts"], t["block_offset"], t["sec_offset"], t["usd"], t["token_amount"])
                for t in tape["trades"]
            ],
        )

    def truncated_pools(self, limit: int) -> list[sqlite3.Row]:
        """Truncated tapes that have not been re-captured yet (one attempt each)."""
        return self.db.execute(
            "SELECT * FROM pools WHERE status='captured' AND truncated=1 AND COALESCE(recaptures, 0) < 1 ORDER BY created_ts DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def replace_capture(self, chain: str, pool: str, tape: dict):
        """Re-captured tape for a pool: drop its old trades, store the new ones, count the attempt."""
        self.db.execute("DELETE FROM trades WHERE chain=? AND pool=?", (chain, pool))
        self.save_capture(chain, pool, tape)
        self.db.execute("UPDATE pools SET recaptures=COALESCE(recaptures, 0)+1 WHERE chain=? AND pool=?", (chain, pool))

    def mark_recaptured(self, chain: str, pool: str):
        self.db.execute("UPDATE pools SET recaptures=COALESCE(recaptures, 0)+1 WHERE chain=? AND pool=?", (chain, pool))

    # ---- snapshots ----

    def due_snapshots(self, chain: str, now: float, ages_min: list[int]) -> dict[int, list[str]]:
        """Captured pools whose +age snapshot is due now. Pools already past the on-time window (the
        collector was down) are skipped: a late snapshot would be stored under the wrong age."""
        due: dict[int, list[str]] = {}
        for age in ages_min:
            late = (age + snapshot_tolerance_min(age)) * 60
            rows = self.db.execute(
                """SELECT p.pool FROM pools p WHERE p.chain=? AND p.status='captured' AND p.created_ts<=? AND p.created_ts>?
                   AND NOT EXISTS (SELECT 1 FROM snapshots s WHERE s.chain=p.chain AND s.pool=p.pool AND s.target_age_min=?)""",
                (chain, now - age * 60, now - late, age),
            ).fetchall()
            if rows:
                due[age] = [r[0] for r in rows]
        return due

    def save_snapshot(self, chain: str, pool: str, age_min: int, snap: dict):
        self.db.execute(
            "INSERT OR REPLACE INTO snapshots (chain, pool, target_age_min, ts, price_usd, reserve_usd, fdv_usd, trades_h1, volume_h1, trades_m30, trades_m15) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (chain, pool, age_min, time.time(), snap.get("price_usd"), snap.get("reserve_usd"), snap.get("fdv_usd"), snap.get("trades_h1"), snap.get("volume_h1"), snap.get("trades_m30"), snap.get("trades_m15")),
        )

    # ---- wallets, alerts, launch info, classes ----

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

    def save_classes(self, stats: dict[str, dict], labels: tuple[str, ...]):
        """Replaces the cached serial-wallet classes the live alerts read (refreshed by housekeeping)."""
        now = time.time()
        self.db.execute("DELETE FROM wallet_classes")
        self.db.executemany(
            "INSERT INTO wallet_classes (wallet, label, launches, updated_ts) VALUES (?,?,?,?)",
            [(w, s["label"], s["launches"], now) for w, s in stats.items() if s["label"] in labels],
        )

    def load_classes(self) -> dict[str, dict]:
        return {r["wallet"]: {"label": r["label"], "launches": r["launches"]} for r in self.db.execute("SELECT * FROM wallet_classes")}

    def save_money(self, summary: dict):
        self.db.execute("INSERT OR REPLACE INTO money (computed_ts, summary_json) VALUES (?,?)", (summary["computed_ts"], json.dumps(summary)))

    def latest_money(self) -> dict | None:
        r = self.db.execute("SELECT summary_json FROM money ORDER BY computed_ts DESC LIMIT 1").fetchone()
        return json.loads(r[0]) if r else None

    # ---- credits, persisted across restarts ----

    def add_credits(self, day: str, n: int):
        if n:
            self.db.execute("INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = v + excluded.v", (f"credits:{day}", n))

    def credits_on(self, day: str) -> float:
        r = self.db.execute("SELECT v FROM meta WHERE k=?", (f"credits:{day}",)).fetchone()
        return r[0] if r else 0.0

    # ---- reads for analysis ----

    def all_trades(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM trades")]

    def all_pools(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM pools")]

    def all_snapshots(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM snapshots")]

    def load_for_analysis(self, snipe_s: int, since: float | None = None) -> dict:
        """What the analysis needs, and no more, so a week of launches fits in memory.

        Only trades of (pool, wallet) pairs where the wallet sniped are loaded (about 40% of rows):
        wallet classes, packs and snipe prices only ever look at those. Per-pool distinct buyer counts
        come from an SQL aggregate instead. `since` limits trades and buyer counts to launches created
        after it; pools are always loaded whole (eligibility needs each token's first pool)."""
        since = since or 0
        trades = [
            dict(r)
            for r in self.db.execute(
                f"""SELECT {TRADE_COLUMNS} FROM trades t
                    JOIN (SELECT DISTINCT chain, pool, wallet FROM trades WHERE kind='buy' AND sec_offset<=?) s
                      ON s.chain=t.chain AND s.pool=t.pool AND s.wallet=t.wallet
                    JOIN pools p ON p.chain=t.chain AND p.pool=t.pool
                    WHERE p.created_ts>=?""",
                (snipe_s, since),
            )
        ]
        buyer_counts = {
            (r[0], r[1]): r[2]
            for r in self.db.execute(
                """SELECT t.chain, t.pool, COUNT(DISTINCT t.wallet) FROM trades t JOIN pools p ON p.chain=t.chain AND p.pool=t.pool
                   WHERE t.kind='buy' AND p.created_ts>=? GROUP BY t.chain, t.pool""",
                (since,),
            )
        }
        return {
            "pools": self.all_pools(),
            "trades": trades,
            "buyer_counts": buyer_counts,
            "snapshots": self.all_snapshots(),
            "info": self.launch_info(),
            "since": since,
        }

    def commit(self):
        self.db.commit()
