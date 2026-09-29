# Agent notes for Serial Sniper Tracker

This repo has two parts:

- `sniper/`: the Serial Sniper Tracker (the main project, see README.md and the section below).
- `bot/` + `core/` + `strategies/`: CoinGecko's onchain-signal-bot starter it was forked from,
  unchanged. `sniper/` reuses `core/client.py`, `core/wallets.py` and `core/links.py`.

## Serial Sniper Tracker

```
sniper/config.py    every tunable, loaded from sniper.yaml
sniper/store.py     SQLite (WAL): pools, trades, snapshots, wallets, launch_info, alerts, wallet_classes, meta (credits/day)
sniper/analyze.py   pure, offline: launch tapes, wallet classes, packs, outcomes (all code-derived labels)
sniper/collect.py   the 24/7 loop: discover -> capture launch tapes -> snapshots -> alerts -> hourly housekeeping
sniper/profile.py   wallet PnL + trade-history profiles, token info enrichment
sniper/report.py    reports/serial-snipers.md
sniper/money.py     cash in vs cash out per serial wallet (CoinGecko wallet PnL, paged; FIFO fallback)
sniper/web.py + sniper/web/  read-only live dashboard (python -m sniper web)
sniper/cli.py       collect, leaderboard, profile, enrich, money, web, report
scripts/            Windows supervisor (run-forever.cmd, start.ps1, stop.ps1)
docs/make_architecture.py  renders docs/architecture.png
tests/test_sniper_analyze.py  offline tests; no network calls
```

- The collector stores raw launch tapes; thresholds are applied at analysis time. Keep it that way
  so `report` can replay new thresholds without spending credits.
- Always analyze through `analyze.prepare()`: it keeps each token's first complete pool and collapses
  same-transaction buy/sell legs. Live alerts read the `wallet_classes` cache that the hourly job
  (`collect.analysis_job`, run in a worker thread) refreshes from `wallet_stats()`.
- `store.load_for_analysis()` loads only the trades of (pool, wallet) pairs that sniped, over the last
  `analysis_days`; anything that needs other rows must add them there deliberately (memory).
- Never present a class, pack, multiple or score as a CoinGecko API field.

## Onchain Signal Bot (the starter)

This is a headless, paper-only CoinGecko API bot. Its scan records are deliberately complete so
the same observations can be replayed later without lookahead.

## Repo map

```
bot/scan.py       discovery, enrichment and scan records
bot/strategy.py   YAML strategy loading and explainable checks
bot/networks.py   validates strategy chain/network ids against a cached GET /onchain/networks
bot/engine.py     paper entries, exits and position metadata
bot/runner.py     forward/autopilot loop, restart-safe state and credit guard
bot/backtest.py   recorded-scan replay and historical exit-rule test
bot/dashboard.py  the branded rich terminal dashboard
bot/recap.py      markdown end-of-run recap
bot/record.py     capture scans for repeatable demos
bot/cli.py        run, backtest, report, article-kit and link commands
core/             shared client, paper engine, reports and article kit
strategies/       YAML strategy examples
tests/            offline tests; no network calls
```

## Commands

```
make install
cp env.example .env
make forward STRATEGY=new-launch-sniff MINUTES=3
make autopilot STRATEGY=new-launch-sniff
make backtest STRATEGY=new-launch-sniff ARGS=--replay-scans
make report RUN=latest
make article-kit RUN=latest HANDLE=you
make set-link HANDLE=you
```

`make forward` is a bounded paper test. `make autopilot` runs until stopped. Set
`NO_WEBSOCKET=1` for REST-only monitoring.

## Strategy and safety

- `strategies/new-launch-sniff.yaml` is the Demo-friendly starting point.
- `strategies/trending-momentum.yaml` adds momentum and safety filters.
- `strategies/smart-money-follow.yaml` adds the Analyst+ top-trader PnL gate.
- `strategies/base-momentum.yaml` is the same trending-momentum idea on Base, proof a strategy
  targets any GeckoTerminal network, not just Solana.
- `strategies/multi-chain-safe-movers.yaml` screens several networks in one megafilter call.
- A strategy's `source.chain` (one network) or `source.networks`/`source.chains` (a list) can name
  **any** GeckoTerminal network id. `bot/networks.py` validates every id at startup against a
  24h-cached `GET /onchain/networks` and fails fast with a clear message on a typo or unsupported id
  — it never silently scans zero candidates from a bad chain id.
- Every scan stores candidate inputs and every check result in `data/scans/`.
- A filter can opt into `filters.allow_unknown: [liquidity, gt_score, volume_24h, txns_24h]` so a
  missing value (routinely true of pools a few seconds old) logs "unknown, allowing" instead of an
  automatic fail — see `bot/filters.py::check_min`. Off by default; only strategies that intentionally
  target very fresh, thinly-indexed pools should turn it on for a given check.
- `backtest --replay-scans` changes thresholds on recorded observations. It does not pretend a
  historical new-pool listing endpoint exists.
- All entries and exits go through `core.paper`; no live executor is included.

## Rules

- Never commit `.env` or print a key.
- Keep the CoinGecko API badge and links block in the README.
- Use the correct official logo variant for the background; never recolor or crop it.
- Verify new endpoint paths and parameters in the CoinGecko API docs first.
- Keep decision reasons in logs so a reader can understand every skip and trade.
