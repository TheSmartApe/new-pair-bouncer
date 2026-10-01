"""Pre-entry checks: should a bot buy this new pair right now? Pure functions, no network calls.

Every check returns a `Check` (pass / warn / fail / unknown + a plain-English reason). The verdict:
  AVOID  any check failed
  WATCH  two or more warnings
  ENTER  otherwise

Checks run cheapest first, so the expensive ones (token info, wallet profiles) are only paid for on
pools that are still candidates:
  stage 0, free, from the launch tape and the bot's memory of past launches:
      wash trading, known bots and entity clusters, the dev's behaviour, crowd and price path
  stage 1, 1 API call:   CoinGecko token info (honeypot, GT Score, dev holding, holder concentration)
  stage 2, ~5 API calls: CoinGecko wallet PnL of the biggest early buyers (fresh wallets, proven traders)

All thresholds live in BouncerConfig. Every label used here (round-tripper, cluster, fresh wallet...)
is computed by this repo from CoinGecko data; none of them are CoinGecko API fields.
"""
from collections import defaultdict
from dataclasses import asdict, dataclass, field

PASS, WARN, FAIL, UNKNOWN = "pass", "warn", "fail", "unknown"


@dataclass
class BouncerConfig:
    # Defaults were chosen on the first half of the recorded launches and checked on the second half
    # (python -m sniper backtest-bouncer). Re-run the backtest before trusting them on another chain.
    # wash trading (from the launch tape)
    max_roundtrip_volume_share: float = 0.90   # fail above: share of window volume from wallets that bought and sold within roundtrip_s
    warn_roundtrip_volume_share: float = 0.77
    roundtrip_s: int = 30
    # bots and entity clusters (from memory)
    max_bot_buyer_share: float = 0.15          # fail above: share of buyers that are known round-trip / dust bots
    warn_bot_buyer_share: float = 0.07
    max_cluster_buyer_share: float = 0.50      # fail above: share of buyers that belong to one coordinated group
    warn_cluster_buyer_share: float = 0.25
    cohort_min_wallets: int = 4                # this many distinct wallets buying in the same block = a coordinated cohort
    # rug ring: wallets that keep showing up early in launches that die
    ring_window_s: int = 30                    # buyers whose first buy came within this many seconds of the first trade
    ring_min_launches: int = 2                 # ...with at least this many earlier launches whose outcome is known
    ring_min_rug_share: float = 0.30           # ...of which at least this share were dead within the hour, fail the pair
    # supply grab: who already holds the token after the first minutes
    max_early_supply_share: float = 0.25       # fail at or above: share of supply still held (bought minus sold) by the wallets that bought in the launch window
    warn_top3_supply_share: float = 0.15       # warn at or above: same, for the 3 biggest of those wallets
    # dev / deployer
    fail_if_dev_sold: bool = True
    max_deployer_rugs: int = 0                 # fail above: earlier pools from this deployer whose liquidity was pulled within an hour
    rug_reserve_usd: float = 100               # an earlier pool with less liquidity than this an hour after launch counts as pulled
    warn_dev_launches: int = 2                 # warn above: launches by the same deployer seen in memory (serial launcher)
    # crowd and price path: buy only launches with a real crowd
    min_buyers: int = 15
    min_reserve_usd: float = 3000
    max_top3_buy_share: float = 0.45           # fail above: share of window buy volume from the 3 biggest buyers
    # token info (stage 1)
    min_gt_score: float = 15
    max_dev_holding_pct: float = 20
    max_top10_holding_pct: float = 70
    # wallet profiles (stage 2)
    profile_top_buyers: int = 5
    fresh_wallet_max_tokens: int = 3           # a wallet that has traded this many tokens or fewer, ever, is fresh
    max_fresh_share: float = 0.6               # fail above: share of profiled top buyers that are fresh wallets
    # paper trading
    min_fill_reserve_usd: float = 300          # no paper position in a pool whose reported liquidity is below this (it can't absorb the trade)
    position_usd: float = 100
    take_profit_pct: float = 100
    stop_loss_pct: float = 50
    max_hold_min: int = 60
    slippage_bps: float = 300
    fee_bps: float = 100

    @classmethod
    def from_dict(cls, d: dict | None) -> "BouncerConfig":
        d = d or {}
        known = set(cls.__dataclass_fields__)
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"unknown bouncer keys: {', '.join(sorted(unknown))}")
        return cls(**d)


@dataclass
class Check:
    key: str
    group: str
    status: str
    reason: str
    value: object = None


@dataclass
class Verdict:
    verdict: str
    checks: list[Check] = field(default_factory=list)
    stage: int = 0
    features: dict = field(default_factory=dict)

    @property
    def fails(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def warns(self) -> list[Check]:
        return [c for c in self.checks if c.status == WARN]

    def to_dict(self) -> dict:
        return {"verdict": self.verdict, "stage": self.stage, "features": self.features, "checks": [asdict(c) for c in self.checks]}


@dataclass
class Memory:
    """What the bot has learned from earlier launches."""

    classes: dict[str, dict] = field(default_factory=dict)     # wallet -> {label, launches}
    packs: dict[str, dict] = field(default_factory=dict)       # wallet -> {pack_id, size, shared_launches}
    dev_launches: dict[str, int] = field(default_factory=dict)  # deployer -> launches seen
    dev_rugs: dict[str, int] = field(default_factory=dict)      # deployer -> earlier pools whose liquidity was pulled within an hour
    wallet_rugs: dict[str, tuple] = field(default_factory=dict)  # wallet -> (earlier launches it bought early, how many of them were dead within the hour)


def _pct(x: float) -> str:
    return f"{100 * x:.0f}%"


def _short(w: str) -> str:
    return f"{w[:6]}…{w[-4:]}"


# ---- features from the launch tape ----


def fallback_dev(trades: list[dict], created_ts: float | None, creation_block_s: int = 2) -> str | None:
    """Who to treat as the dev when token info has no developer_address: the buyer in the pool's
    creation block, but only when there is exactly one and the first trade landed within
    `creation_block_s` of pool creation. A pool that opened late, or whose block 0 had several
    buyers, has no reliable stand-in, so the dev is unknown. Store.deployers() uses the same rule."""
    if created_ts is None or not trades:
        return None
    if min(t["ts"] for t in trades) - created_ts > creation_block_s:
        return None
    block0 = {t["wallet"] for t in trades if t["kind"] == "buy" and t["block_offset"] == 0}
    return next(iter(block0)) if len(block0) == 1 else None


def tape_features(trades: list[dict], cfg: BouncerConfig, developer: str | None = None, supply: float | None = None,
                  raw_trades: list[dict] | None = None, created_ts: float | None = None, creation_block_s: int = 2) -> dict:
    """Everything stage 0 needs, from the cleaned launch tape of ONE pool (same-tx legs collapsed).
    `supply` is the token's total supply (CoinGecko tokens/multi normalized_total_supply), for the
    supply-grab check. `raw_trades` (the tape before same-tx legs were collapsed) is used for net
    token holdings, so a buy and a sell in one transaction net out. `created_ts` lets the creation-block
    buyer stand in for an unknown developer (see fallback_dev)."""
    buys = [t for t in trades if t["kind"] == "buy"]
    sells = [t for t in trades if t["kind"] == "sell"]
    buyers = {t["wallet"] for t in buys}
    volume = sum(t["usd"] or 0 for t in trades)
    first_buy: dict[str, float] = {}
    for t in sorted(buys, key=lambda t: t["ts"]):
        first_buy.setdefault(t["wallet"], t["ts"])
    roundtrippers = {t["wallet"] for t in sells if t["wallet"] in first_buy and 0 <= t["ts"] - first_buy[t["wallet"]] <= cfg.roundtrip_s}
    rt_volume = sum(t["usd"] or 0 for t in trades if t["wallet"] in roundtrippers)
    by_block: dict[int, set[str]] = defaultdict(set)
    for t in buys:
        if t["block_offset"] > 0:  # block 0 is the creator's own block
            by_block[t["block"]].add(t["wallet"])
    cohort = max(by_block.values(), key=len) if by_block else set()
    buy_usd: dict[str, float] = defaultdict(float)
    for t in buys:
        buy_usd[t["wallet"]] += t["usd"] or 0
    total_buy = sum(buy_usd.values())
    top3 = sum(sorted(buy_usd.values(), reverse=True)[:3])
    priced = [t for t in sorted(trades, key=lambda t: (t["block"], t["ts"])) if t.get("token_amount")]
    first_price = (priced[0]["usd"] / priced[0]["token_amount"]) if priced else None
    last_price = (priced[-1]["usd"] / priced[-1]["token_amount"]) if priced else None
    net: dict[str, float] = defaultdict(float)  # tokens still held by each launch-window buyer
    for t in raw_trades if raw_trades is not None else trades:
        if t["wallet"] in buyers:
            net[t["wallet"]] += (t.get("token_amount") or 0) * (1 if t["kind"] == "buy" else -1)
    held = sorted((max(v, 0.0) for v in net.values()), reverse=True)
    early = sorted({t["wallet"] for t in buys if t["sec_offset"] is not None and t["sec_offset"] <= cfg.ring_window_s})
    dev = (developer or "").lower() or None
    if dev is None:  # no token info yet: the single creation-block buyer stands in for the dev
        dev = fallback_dev(trades, created_ts, creation_block_s)
    return {
        "trades": len(trades),
        "buyers": len(buyers),
        "buyer_set": buyers,
        "volume_usd": round(volume, 2),
        "roundtrippers": roundtrippers,
        "roundtrip_volume_share": round(rt_volume / volume, 3) if volume else 0.0,
        "cohort": cohort,
        "top3_buy_share": round(top3 / total_buy, 3) if total_buy else 0.0,
        "buy_usd": dict(buy_usd),
        "first_price": first_price,
        "last_price": last_price,
        "pump_multiple": (last_price / first_price) if first_price and last_price else None,
        "dev": dev,
        "dev_sold": bool(dev and any(t["wallet"] == dev for t in sells)),
        "dev_from_token_info": bool(developer),
        "early_wallets": early,
        "supply": supply,
        "early_supply_share": round(sum(held) / supply, 4) if supply else None,
        "top3_supply_share": round(sum(held[:3]) / supply, 4) if supply else None,
    }


# ---- stage 0 ----


def stage0(f: dict, mem: Memory, cfg: BouncerConfig, reserve_usd: float | None = None) -> list[Check]:
    out: list[Check] = []
    buyers = f["buyer_set"]
    n = len(buyers) or 1

    # wash trading
    share = f["roundtrip_volume_share"]
    status = FAIL if share > cfg.max_roundtrip_volume_share else WARN if share > cfg.warn_roundtrip_volume_share else PASS
    out.append(Check("wash_trading", "wash", status, f"{_pct(share)} of the volume came from wallets that bought and sold within {cfg.roundtrip_s}s ({len(f['roundtrippers'])} wallets)", share))

    # rug ring: the strongest check in the backtest. Rugs on this chain come from a recurring ring of
    # wallets (launch wallets plus the bots that snipe their launches), and their addresses repeat.
    ring = []
    for w in f.get("early_wallets", []):
        n_launches, n_rugs = mem.wallet_rugs.get(w, (0, 0))
        if n_launches >= cfg.ring_min_launches and n_rugs / n_launches >= cfg.ring_min_rug_share:
            ring.append((n_rugs, n_launches))
    if ring:
        worst = max(ring)
        out.append(Check("rug_ring", "clusters", FAIL,
                         f"{len(ring)} of the first-{cfg.ring_window_s}s buyers were early in launches that died: one of them in {worst[0]} of {worst[1]}", len(ring)))
    else:
        out.append(Check("rug_ring", "clusters", PASS, "none of the first buyers has a record of early buys in launches that died", 0))

    # known bots
    bots = [w for w in buyers if mem.classes.get(w, {}).get("label") in ("round_tripper", "dust_bot")]
    share = len(bots) / n
    status = FAIL if share > cfg.max_bot_buyer_share else WARN if share > cfg.warn_bot_buyer_share else PASS
    out.append(Check("known_bots", "clusters", status, f"{len(bots)} of {len(buyers)} buyers are known round-trip or dust bots from earlier launches", round(share, 3)))

    # entity clusters: a known pack, or a fresh same-block cohort
    packs: dict[str, set[str]] = defaultdict(set)
    for w in buyers:
        p = mem.packs.get(w)
        if p:
            packs[p["pack_id"]].add(w)
    biggest_pack = max(packs.items(), key=lambda kv: len(kv[1])) if packs else (None, set())
    cohort = f["cohort"] if len(f["cohort"]) >= cfg.cohort_min_wallets else set()
    cluster = biggest_pack[1] if len(biggest_pack[1]) >= len(cohort) else cohort
    share = len(cluster) / n
    if biggest_pack[0] and cluster is biggest_pack[1]:
        meta = mem.packs[next(iter(cluster))]
        reason = f"{len(cluster)} buyers belong to one known cluster ({meta['size']} wallets that bought {meta['shared_launches']} earlier launches together)"
    elif cohort:
        reason = f"{len(cohort)} different wallets bought in the same block: a coordinated cohort"
    else:
        reason = "no coordinated group of buyers"
    status = FAIL if share > cfg.max_cluster_buyer_share else WARN if share > cfg.warn_cluster_buyer_share else PASS
    out.append(Check("entity_cluster", "clusters", status, reason, round(share, 3)))

    # dev
    if f["dev"] and f["dev_sold"] and cfg.fail_if_dev_sold:
        who = "the dev" if f.get("dev_from_token_info") else "the creation-block buyer (usually the dev)"
        out.append(Check("dev_sold", "dev", FAIL, f"{who} already sold in the first minutes", True))
    elif f["dev"]:
        out.append(Check("dev_sold", "dev", PASS, "the dev has not sold", False))
    else:
        out.append(Check("dev_sold", "dev", UNKNOWN, "no dev buy seen", None))
    rugs = mem.dev_rugs.get(f["dev"] or "", 0)
    if f["dev"]:
        status = FAIL if rugs > cfg.max_deployer_rugs else PASS
        out.append(Check("deployer_rugs", "dev", status, f"this launch wallet's liquidity was gone within an hour on {rugs} earlier pools" if rugs else "no pulled liquidity in this launch wallet's earlier pools", rugs))
    launches = mem.dev_launches.get(f["dev"] or "", 0)
    status = WARN if launches > cfg.warn_dev_launches else PASS
    out.append(Check("serial_launcher", "dev", status, f"this deployer launched {launches} other tokens the bot has seen" if launches else "first launch the bot has seen from this deployer", launches))

    # supply grab
    share = f.get("early_supply_share")
    if share is not None:
        status = FAIL if share >= cfg.max_early_supply_share else PASS
        out.append(Check("supply_grab", "holders", status, f"the wallets that bought in the first minutes still hold {_pct(share)} of the supply", share))
        top3 = f["top3_supply_share"]
        out.append(Check("top3_supply", "holders", WARN if top3 >= cfg.warn_top3_supply_share else PASS, f"the 3 biggest early buyers hold {_pct(top3)} of the supply", top3))
    else:
        out.append(Check("supply_grab", "holders", UNKNOWN, "total supply unknown", None))

    # crowd and price path
    status = FAIL if f["buyers"] < cfg.min_buyers else PASS
    out.append(Check("buyers", "market", status, f"{f['buyers']} unique buyers", f["buyers"]))
    if reserve_usd is not None:
        status = FAIL if reserve_usd < cfg.min_reserve_usd else PASS
        out.append(Check("liquidity", "market", status, f"${reserve_usd:,.0f} of liquidity", round(reserve_usd, 2)))
    else:
        out.append(Check("liquidity", "market", UNKNOWN, "liquidity not reported for this pool", None))
    pm = f["pump_multiple"]
    if pm is not None:  # informational: in the backtest, launches that had already run up did not do worse
        out.append(Check("price_move", "market", PASS, f"price is {pm:.1f}x the first trade", round(pm, 2)))
    status = FAIL if f["top3_buy_share"] > cfg.max_top3_buy_share else PASS
    out.append(Check("buy_concentration", "market", status, f"the 3 biggest buyers did {_pct(f['top3_buy_share'])} of the buying", f["top3_buy_share"]))
    return out


# ---- stage 1: CoinGecko token info ----


def stage1(info: dict | None, cfg: BouncerConfig) -> list[Check]:
    if not info:
        return [Check("token_info", "token", UNKNOWN, "token info not indexed yet", None)]
    out: list[Check] = []
    hp = info.get("is_honeypot")
    if hp in (True, "yes", "true"):
        out.append(Check("honeypot", "token", FAIL, "flagged as a honeypot", True))
    elif hp in (False, "no", "false"):
        out.append(Check("honeypot", "token", PASS, "not a honeypot", False))
    else:
        out.append(Check("honeypot", "token", UNKNOWN, "honeypot status unknown", None))
    gt = info.get("gt_score")
    if gt is not None:
        out.append(Check("gt_score", "token", WARN if float(gt) < cfg.min_gt_score else PASS, f"GT Score {float(gt):.0f}", float(gt)))
    dev_pct = info.get("developer_holding_percentage")
    if dev_pct not in (None, ""):
        v = float(dev_pct)
        out.append(Check("dev_holding", "token", FAIL if v > cfg.max_dev_holding_pct else PASS, f"the dev holds {v:.1f}% of supply", v))
    top10 = (((info.get("holders") or {}).get("distribution_percentage") or {}).get("top_10"))
    if top10 not in (None, ""):
        v = float(top10)
        out.append(Check("top10_holders", "token", WARN if v > cfg.max_top10_holding_pct else PASS, f"the top 10 holders own {v:.0f}%", v))
    return out


# ---- stage 2: CoinGecko wallet PnL of the biggest early buyers ----


def top_buyers(f: dict, cfg: BouncerConfig) -> list[str]:
    return [w for w, _ in sorted(f["buy_usd"].items(), key=lambda kv: -kv[1])[: cfg.profile_top_buyers]]


def stage2(profiles: dict[str, dict], cfg: BouncerConfig) -> list[Check]:
    """profiles: wallet -> {total_tokens, realized_usd} from GET /onchain/wallets/{address}/pnl."""
    known = {w: p for w, p in profiles.items() if p and p.get("total_tokens") is not None}
    if not known:
        return [Check("fresh_wallets", "traders", UNKNOWN, "no wallet profiles", None)]
    fresh = [w for w, p in known.items() if p["total_tokens"] <= cfg.fresh_wallet_max_tokens]
    share = len(fresh) / len(known)
    out = [Check("fresh_wallets", "traders", FAIL if share > cfg.max_fresh_share else PASS,
                 f"{len(fresh)} of the {len(known)} biggest buyers are fresh wallets (≤{cfg.fresh_wallet_max_tokens} tokens ever traded)", round(share, 3))]
    proven = [w for w, p in known.items() if (p.get("realized_usd") or 0) > 1000 and p["total_tokens"] > 20]
    out.append(Check("proven_traders", "traders", PASS, f"{len(proven)} of the biggest buyers have made $1K+ trading before" if proven else "no proven traders among the biggest buyers", len(proven)))
    return out


def usable_reserve(reserve_usd: float | None, recent_trades: int | None, cfg: BouncerConfig) -> float | None:
    """The pool liquidity to trust, or None when it can't be trusted.

    A quiet pool's reported reserve is what is really there: when the liquidity provider pulls, the
    pool shows ~$0 and no more trades, and selling into it returns ~nothing whatever its last price
    says. An ACTIVE pool reporting under `rug_reserve_usd` is a CoinGecko misreport (some live Uniswap
    v3/v4 pools show $0 while trading thousands a minute), so it is treated as unknown."""
    if reserve_usd is None:
        return None
    if not recent_trades:
        return max(reserve_usd, 0.0)
    if reserve_usd < cfg.rug_reserve_usd:
        return None
    return reserve_usd


def only_stand_in_dev_failed(found: list[Check], f: dict) -> bool:
    """Stage 0 failed only on dev checks judged against a stand-in dev: worth paying for token info to
    learn the real developer before saying no."""
    fails = [c for c in found if c.status == FAIL]
    return bool(fails) and not f.get("dev_from_token_info") and all(c.group == "dev" for c in fails)


def crowd_only(f: dict, cfg: BouncerConfig) -> bool:
    """The crowd-size rule alone (enough buyers, buying not concentrated in 3 wallets). The 'crowd' paper
    book buys every pair that passes just this, so the gap between it and the bouncer book is what the
    wallet, dev and supply checks add on top of picking busy launches."""
    return f["buyers"] >= cfg.min_buyers and f["top3_buy_share"] <= cfg.max_top3_buy_share


def decide(checks: list[Check], stage: int, features: dict | None = None) -> Verdict:
    fails = [c for c in checks if c.status == FAIL]
    warns = [c for c in checks if c.status == WARN]
    verdict = "AVOID" if fails else "WATCH" if len(warns) >= 2 else "ENTER"
    feats = {k: v for k, v in (features or {}).items() if not isinstance(v, (set, dict))}
    return Verdict(verdict, checks, stage, feats)
