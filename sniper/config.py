"""Every tunable for the serial sniper tracker, loaded from sniper.yaml with sane defaults.

The collector records the raw launch-window tape (every trade in the first `window_s` seconds of
each pool). Thresholds like `snipe_s` or `min_launches` are applied at analysis time, so they can be
changed and re-run against the same recorded data without spending credits again.
"""
from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "sniper.yaml"
DB_PATH = ROOT / "data" / "sniper.db"
REPORTS_DIR = ROOT / "reports"


@dataclass
class SniperConfig:
    # --- collection ---
    chains: list[str] = field(default_factory=lambda: ["robinhood"])
    interval_s: int = 120          # seconds between discovery sweeps
    max_new_pool_pages: int = 10   # 20 pools/page; ~16 min of Robinhood launches at 10 pages
    window_s: int = 120            # launch tape recorded per pool, from pool creation
    lag_s: int = 90                # wait this long after the window closes so trades are indexed
    max_trade_pages: int = 5       # trades/range pages per pool (newest first, so truncation drops the earliest trades)
    min_buys: int = 3              # skip pools with fewer buys: no launch, nothing to snipe
    snapshot_ages_min: list[int] = field(default_factory=lambda: [60, 360, 1440])
    max_pending_age_min: int = 60  # give up on pools we never got to within this age
    housekeeping_every_min: int = 60  # while collecting: enrich new launches (token info) and rewrite the report
    handle: str | None = None      # utm_content on CoinGecko links in the report (your X handle)

    # --- analysis (code-derived labels, not CoinGecko fields) ---
    snipe_s: int = 10              # a buy within this many seconds of the pool's first trade is a snipe
    min_launches: int = 3          # sniping at least this many distinct launches makes a wallet "serial"
    roundtrip_max_s: int = 30      # a snipe sold again within this many seconds is a round trip
    roundtrip_min_rate: float = 0.7  # a serial wallet that round-trips at least this share of its launches is a round-tripper
    pack_min_shared: int = 3       # two serial wallets sharing at least this many launches...
    pack_min_overlap: float = 0.5  # ...with shared / union of their launches (Jaccard) at least this...
    pack_max_block_gap: int = 3    # ...and buying a median of at most this many blocks apart form a pack
    bundler_prefixes: list[str] = field(default_factory=lambda: ["0x4337"])  # ERC-4337 bundler EOAs submit other people's trades
    max_launches_per_hour: float | None = None  # optional: above this rate a wallet is set aside as infrastructure (off by default: high-rate farms are the story)
    alive_min_trades_h1: int = 1   # a pool with at least this many trades in the last hour at snapshot time counts as alive

    @classmethod
    def load(cls, path: Path | None = None) -> "SniperConfig":
        path = path or DEFAULT_CONFIG
        data = {}
        if path.exists():
            data = yaml.safe_load(path.read_text()) or {}
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown keys in {path.name}: {', '.join(sorted(unknown))}")
        return cls(**data)
