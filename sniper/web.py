"""Read-only live dashboard: `python -m sniper web`, then open http://localhost:8765.

Runs next to the collector and only reads the database (WAL mode, no locks held). One JSON endpoint,
/api/state, feeds a static page in sniper/web/ that polls it every few seconds. No API key ever
reaches this process's responses: it makes no CoinGecko calls at all.
"""
import json
import mimetypes
import sqlite3
import threading
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import analyze
from .config import ROOT, SniperConfig

STATIC = Path(__file__).resolve().parent / "web"
BRAND = ROOT / "core" / "brand"
FEED_ROWS = 30
BOTS = {"round_tripper"}  # the "round-trip bots" everywhere on the dashboard, matching the report


class State:
    """Builds the /api/state payload. Cheap parts are recomputed on every request, the 24h rates
    every `slow_ttl` seconds."""

    def __init__(self, db_path: Path, cfg: SniperConfig, slow_ttl: float = 60):
        self.db_path = db_path
        self.cfg = cfg
        self.slow_ttl = slow_ttl
        self._slow: dict = {}
        self._slow_ts = 0.0
        self._lock = threading.Lock()

    def _db(self):
        db = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def _classes(self, db) -> dict[str, dict]:
        return {r["wallet"]: {"label": r["label"], "launches": r["launches"]} for r in db.execute("SELECT wallet, label, launches FROM wallet_classes")}

    def _feed(self, db, classes) -> list[dict]:
        pools = [dict(r) for r in db.execute(
            "SELECT chain, pool, token, name, dex, created_ts, launch_ts FROM pools WHERE status='captured' AND COALESCE(truncated,0)=0 ORDER BY created_ts DESC LIMIT ?",
            (FEED_ROWS,),
        )]
        if not pools:
            return []
        marks = ",".join("?" * len(pools))
        rows = [dict(r) for r in db.execute(f"SELECT * FROM trades WHERE pool IN ({marks})", [p["pool"] for p in pools])]
        rows = analyze.drop_fee_legs(rows, self.cfg)
        devs = {r["pool"]: (r["developer"] or "").lower() for r in db.execute(f"SELECT pool, developer FROM launch_info WHERE pool IN ({marks})", [p["pool"] for p in pools])}
        by_pool: dict[str, list[dict]] = defaultdict(list)
        for t in rows:
            by_pool[t["pool"]].append(t)
        out = []
        for p in pools:
            trades = by_pool.get(p["pool"], [])
            buyers = {t["wallet"] for t in trades if t["kind"] == "buy"}
            first10 = {t["wallet"] for t in trades if t["kind"] == "buy" and t["sec_offset"] <= self.cfg.snipe_s}
            snipe_usd = sum(t["usd"] or 0 for t in trades if t["kind"] == "buy" and t["sec_offset"] <= self.cfg.snipe_s)
            serial = sorted((w for w in first10 if classes.get(w, {}).get("label") == "serial_sniper"), key=lambda w: -classes[w]["launches"])
            bots = [w for w in buyers if classes.get(w, {}).get("label") in BOTS]
            launcher = next((w for w in first10 if classes.get(w, {}).get("label") == "serial_launcher" or w == devs.get(p["pool"])), None)
            out.append({
                "pool": p["pool"],
                "name": p["name"],
                "dex": p["dex"],
                "created_ts": p["created_ts"],
                "buyers": len(buyers),
                "first10": len(first10),
                "snipe_usd": round(snipe_usd, 2),
                "serial_snipers": len(serial),
                "top_sniper": {"wallet": serial[0], "launches": classes[serial[0]]["launches"]} if serial else None,
                "bots": len(bots),
                "bot_share": round(len(bots) / len(buyers), 3) if buyers else 0,
                "launcher": bool(launcher),
            })
        return out

    def _alerts(self, db) -> list[dict]:
        out = []
        for r in db.execute("SELECT ts, pool, payload_json FROM alerts ORDER BY ts DESC LIMIT 12"):
            payload = json.loads(r["payload_json"])
            snipers = [s for s in payload.get("snipers", []) if s.get("label") == "serial_sniper"]
            if not snipers:
                continue
            s = max(snipers, key=lambda s: s.get("prior_launches") or 0)
            out.append({"ts": r["ts"], "name": payload.get("name"), "wallet": s["wallet"], "prior_launches": s.get("prior_launches"),
                        "block_offset": s.get("block_offset"), "sec_offset": s.get("sec_offset"), "count": len(snipers)})
        return out[:8]

    def _slow_part(self, db, classes) -> dict:
        """Share of the last 24h's launches that had a serial sniper / round-trip bots, using the
        collector's cached classes. Cheap SQL, cached for `slow_ttl`."""
        since = time.time() - 86400
        launches = db.execute("SELECT COUNT(*) FROM pools WHERE status='captured' AND COALESCE(truncated,0)=0 AND created_ts>=?", (since,)).fetchone()[0]
        per_pool: dict[str, set[str]] = defaultdict(set)
        for r in db.execute(
            """SELECT t.pool, t.wallet, MIN(t.sec_offset) AS s FROM trades t JOIN pools p ON p.chain=t.chain AND p.pool=t.pool
               WHERE t.kind='buy' AND p.status='captured' AND COALESCE(p.truncated,0)=0 AND p.created_ts>=? GROUP BY t.pool, t.wallet""",
            (since,),
        ):
            label = classes.get(r["wallet"], {}).get("label")
            if label == "serial_sniper" and r["s"] <= self.cfg.snipe_s:
                per_pool[r["pool"]].add("sniper")
            elif label in BOTS:
                per_pool[r["pool"]].add("bot")
        counts = defaultdict(int)
        for c in classes.values():
            counts[c["label"]] += 1
        return {
            "launches_24h": launches,
            "with_sniper_pct": round(100 * sum(1 for v in per_pool.values() if "sniper" in v) / launches, 1) if launches else None,
            "with_bot_pct": round(100 * sum(1 for v in per_pool.values() if "bot" in v) / launches, 1) if launches else None,
            "classes": dict(counts),
        }

    def build(self) -> dict:
        db = self._db()
        try:
            classes = self._classes(db)
            now = time.time()
            with self._lock:
                if now - self._slow_ts > self.slow_ttl:
                    self._slow = self._slow_part(db, classes)
                    self._slow_ts = now
                slow = dict(self._slow)
            day = time.strftime("%Y-%m-%d", time.gmtime(now))
            credits = db.execute("SELECT v FROM meta WHERE k=?", (f"credits:{day}",)).fetchone()
            first, last_seen, pools_seen = db.execute("SELECT MIN(created_ts), MAX(seen_ts), COUNT(*) FROM pools").fetchone()
            captured = db.execute("SELECT COUNT(*) FROM pools WHERE status='captured'").fetchone()[0]
            alerts_total = db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
            money = db.execute("SELECT summary_json FROM money ORDER BY computed_ts DESC LIMIT 1").fetchone()
            return {
                "now": now,
                "chains": self.cfg.chains,
                "interval_s": self.cfg.interval_s,
                "snipe_s": self.cfg.snipe_s,
                "tracking_since": first,
                "last_sweep": last_seen,
                "pools_seen": pools_seen,
                "launches_captured": captured,
                "alerts_total": alerts_total,
                "credits_today": credits[0] if credits else 0,
                "last24": slow,
                "feed": self._feed(db, classes),
                "alerts": self._alerts(db),
                "money": json.loads(money[0]) if money else None,
            }
        finally:
            db.close()


def make_handler(state: State):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the terminal quiet during recordings
            pass

        def _send(self, body: bytes, ctype: str, status: int = 200):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/state":
                try:
                    return self._send(json.dumps(state.build()).encode(), "application/json")
                except sqlite3.Error as exc:
                    return self._send(json.dumps({"error": str(exc)}).encode(), "application/json", 503)
            if path in ("/", "/index.html"):
                target = STATIC / "index.html"
            elif path.startswith("/brand/"):
                target = BRAND / Path(path).name
            else:
                target = STATIC / Path(path).name
            if not target.is_file():
                return self._send(b"not found", "text/plain", 404)
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if target.suffix == ".svg":
                ctype = "image/svg+xml"
            return self._send(target.read_bytes(), ctype)

    return Handler


def serve(db_path: Path, cfg: SniperConfig, port: int = 8765):
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(State(db_path, cfg)))
    print(f"dashboard on http://localhost:{port}  (read-only; Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
