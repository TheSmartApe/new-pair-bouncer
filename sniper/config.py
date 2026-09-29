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
    lag_s: int = 150               # wait this long after the window closes so trades are indexed (indexing comes in batches)
    late_open_s: int = 30          # first trade later than this after pool creation = late opener: re-anchor the tape on the first trade
    quiet_recheck_min: int = 15    # pools with too few buys (or no trades yet) are re-checked each sweep until this age
    max_trade_pages: int = 30      # trades/range pages per pool (100 trades each, newest first); a tape still cut is flagged and excluded
    deep_discovery_every: int = 3  # every Nth sweep, page new_pools back `deep_discovery_min` minutes to catch pools indexed late
    deep_discovery_min: int = 15
    recapture_per_hour: int = 30   # housekeeping re-fetches this many truncated tapes per hour
    min_buys: int = 3              # skip pools with fewer buys: no launch, nothing to snipe
    snapshot_ages_min: list[int] = field(default_factory=lambda: [60, 360, 1440])
    max_credits_per_day: int | None = 60000  # counted in the database per UTC day; the collector pauses past it
    analysis_days: int | None = 7   # classes, packs and the report look at launches from this many days back (memory)
    max_pending_age_min: int = 60  # give up on pools we never got to within this age
    housekeeping_every_min: int = 60  # while collecting: enrich new launches (token info) and rewrite the report
    handle: str | None = None      # utm_content on CoinGecko links in the report (your X handle)

    # --- analysis (code-derived labels, not CoinGecko fields) ---
    snipe_s: int = 10              # a buy within this many seconds of the pool's first trade is a snipe
    min_launches: int = 3          # sniping at least this many distinct launches makes a wallet "serial"
    creation_block_s: int = 2      # without a known developer, a block-0 buy counts as the launcher only if block 0 is within this many seconds of pool creation
    dust_usd: float = 1.0          # serial wallets with a median snipe below this are dust bots
    roundtrip_max_s: int = 30      # a snipe sold again within this many seconds is a round trip
    roundtrip_min_rate: float = 0.7  # a serial wallet that round-trips at least this share of its launches is a round-tripper...
    breakeven_tolerance: float = 0.02  # ...unless those round trips made more than this share of what it bought
    timer_tolerance_s: int = 2     # sells within this many seconds of the wallet's median hold in 80%+ of launches = timer seller
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


def snapshot_tolerance_min(age_min: int) -> float:
    """How late a snapshot may be taken and still count as the +age one."""
    return max(5.0, 0.1 * age_min)
