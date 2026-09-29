<picture>
  <source media="(prefers-color-scheme: dark)" srcset="core/brand/coingecko-api-on-dark.svg">
  <img src="core/brand/coingecko-api-on-light.svg" alt="Data powered by CoinGecko API" height="32">
</picture>

# Serial Sniper Tracker

Robinhood Chain gets hundreds of new token launches an hour. Most of them draw buyers within a few
seconds, but a lot of those buyers are the same wallets every time: snipers that hit every launch,
bots that buy and dump within seconds to fill the buyer list, and devs that relaunch the same
ticker over and over.

This tool records the first two minutes of every launch, remembers every wallet across all of
them, and tells you who keeps showing up, who moves together, and what happens to the launches
they touch. It runs 24/7 on CoinGecko API data.

![How it works](docs/architecture.png)

## Why a screener can't do this

A screener shows you one pool at a time, right now. This question needs memory: the first seconds
of *every* launch, kept long enough to notice that the same 15 wallets were in 18 of them, in the
same block, selling 7 seconds later every time. The tool builds that memory from three CoinGecko
API onchain calls: new pools, pool trades by time range, and pool snapshots. Your own rules then
turn it into classes, packs, alerts and outcome stats.

## What you get

- **Live alerts** when a wallet already known as a serial sniper shows up in the first seconds of a new launch.
- **Three leaderboards**:
  - **serial snipers** buy within 10 seconds of the first trade on 3+ launches and hold.
  - **round-trippers** buy in 10 seconds and sell again within 30 seconds, at a loss, launch after launch.
  - **serial launchers** buy in the launch block itself, which on launchpads is usually the dev.
- **Packs**: serial wallets that keep sniping the same launches, a median of at most 3 blocks apart.
- **Outcomes**: every recorded launch is re-checked at +1h, +6h and +24h. Is it still trading? Where is the price versus what the snipers paid? The report compares launches with and without serial snipers, and with and without round-trippers.
- **The devs behind it**: for launchpad tokens, which developer addresses keep drawing round-trippers.
- **An hourly markdown report** at `reports/serial-snipers.md`, rewritten automatically while the collector runs.

## Get an API key

Start at [coingecko.com/en/api](https://www.coingecko.com/en/api?utm_source=github&utm_content=the_smart_ape).
The launch tape uses `trades/range` and cursor pagination and the wallet profiles use the wallet
endpoints, so plan on the Analyst plan or above.

## Quickstart

```sh
uv venv --python 3.12 .venv
uv pip install -e ".[dev]"
cp env.example .env          # add COINGECKO_API_KEY off-screen; keep COINGECKO_ENVIRONMENT=pro

# record 30 minutes of launches, then read what it found
.venv/Scripts/python -m sniper collect --minutes 30      # macOS/Linux: .venv/bin/python
.venv/Scripts/python -m sniper leaderboard
.venv/Scripts/python -m sniper report
```

Run it unattended and let the outcome snapshots fill in:

```powershell
# Windows: a hidden supervisor that restarts the collector if it ever exits
powershell -ExecutionPolicy Bypass -File scripts/start.ps1
powershell -ExecutionPolicy Bypass -File scripts/stop.ps1
```

```sh
# macOS/Linux
make sniper-autopilot     # nohup, logs to data/collect.log
make sniper-report
```

Everything lands in `data/sniper.db` (SQLite). Stopping and restarting resumes where it left off.

## Commands

| command | what it does | credits |
|---|---|---|
| `python -m sniper collect [--minutes N] [--max-credits N]` | discover launches, record launch tapes, snapshot outcomes, raise alerts, and every hour fetch launch info and rewrite the report | ~15-20K a day on Robinhood Chain |
| `python -m sniper leaderboard` | print serial wallets by class, and packs | 0 |
| `python -m sniper report [--handle you]` | write `reports/serial-snipers.md` | 0 |
| `python -m sniper enrich` | developer and launchpad info for launches that drew serial wallets | 1 per launch |
| `python -m sniper profile --top 20` | wallet PnL plus recent trade history for the top serial snipers and round-trippers | ~4 per wallet |

`leaderboard` and `report` read only the database. Change `snipe_s`, `min_launches` or any pack
setting and re-run them against the same recorded data without spending credits.

## Settings

`sniper.yaml` at the repo root. Every key is optional (defaults in `sniper/config.py`):

```yaml
chains: [robinhood]      # any GeckoTerminal network id: base, eth, bsc, arc, ...
interval_s: 120          # seconds between sweeps
window_s: 120            # seconds of trades recorded per launch
snipe_s: 10              # a buy within this many seconds of the first trade is a snipe
min_launches: 3          # snipes on this many launches make a wallet serial
roundtrip_max_s: 30      # a snipe sold within this many seconds is a round trip
pack_max_block_gap: 3    # pack members buy a median of at most this many blocks apart
snapshot_ages_min: [60, 360, 1440]
```

## CoinGecko data vs. what this repo computes

CoinGecko API supplies the underlying data: new pools, every trade with its block, timestamp,
sender and USD size, pool prices, liquidity and activity, token info (developer address, GT Score,
launchpad details), and wallet PnL and trade history.

Everything else is computed in this repo from that data: snipe, round trip, serial sniper,
round-tripper, serial launcher, packs, alive, and the price multiples. These are editable
inferences, not CoinGecko API fields, official classifications or judgments about anyone behind
a wallet. Read the raw trades before you build a claim on a label.

## Data notes

- **Trades are attributed to the transaction sender.** Addresses starting with `0x4337` are
  ERC-4337 bundlers submitting other users' trades, so they are set aside. Smart-account snipers
  routed through a bundler are not counted.
- **The tracker only sees launches while it runs.** Launch tapes are recorded about 3.5 minutes
  after each pool is created. The collector pages new pools until it reaches pools it has already
  seen, so short restarts leave no gaps. Wallet trade history reaches back about a week, so run
  `profile` while the launches are fresh.
- **Multiples are not PnL.** A multiple compares a later pool price with the average price paid in
  the snipe window. It is not what any wallet realized.
- **Other chains**: set `chains` to any GeckoTerminal network id. Ethereum, Base, BNB Chain,
  Robinhood Chain and Arc Chain have the full wallet feature set.

## Make it yours

Ask Claude Code or Codex:

- "Read AGENTS.md, then add a class for wallets that snipe and are still holding after 24h, with an offline test."
- "Run the tracker on Base as well and add a per-chain comparison table to the report."
- "Send the live alerts to a Telegram bot, rate-limited to one message per launch."
- "Add a 'relaunch' view: the same ticker launched 3+ times in an hour, with who sniped each one."

## Also in this repo

This project is a fork of CoinGecko's
[onchain-signal-bot](https://github.com/cg-brianlsh/onchain-signal-bot) starter, and the original
paper-trading bot still ships here unchanged (`bot/`, `strategies/`). See
[docs/onchain-signal-bot.md](docs/onchain-signal-bot.md).

## Links

<!-- coingecko-links:start -->
- CoinGecko API: https://www.coingecko.com/en/api?utm_source=github&utm_content=the_smart_ape
- Pricing: https://www.coingecko.com/en/api/pricing?utm_source=github&utm_content=the_smart_ape
- Docs: https://docs.coingecko.com?utm_source=github&utm_content=the_smart_ape
- Agent Skill + MCP: https://docs.coingecko.com/ai-integration?utm_source=github&utm_content=the_smart_ape
<!-- coingecko-links:end -->

Data comes from the CoinGecko API. This is a research tool: it places no trades, and nothing in it
is trading advice.
