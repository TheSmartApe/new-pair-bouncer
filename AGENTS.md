# Agent notes for Onchain Signal Bot

This is a headless, paper-only CoinGecko API bot. Its scan records are deliberately complete so
the same observations can be replayed later without lookahead.

## Repo map

```
bot/scan.py       discovery, enrichment and scan records
bot/strategy.py   YAML strategy loading and explainable checks
bot/engine.py     paper entries, exits and position metadata
bot/runner.py     forward/autopilot loop, restart-safe state and credit guard
bot/backtest.py   recorded-scan replay and historical exit-rule test
bot/dashboard.py  rich terminal dashboard
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
- Every scan stores candidate inputs and every check result in `data/scans/`.
- `backtest --replay-scans` changes thresholds on recorded observations. It does not pretend a
  historical new-pool listing endpoint exists.
- All entries and exits go through `core.paper`; no live executor is included.

## Rules

- Never commit `.env` or print a key.
- Keep the CoinGecko API badge and links block in the README.
- Use the correct official logo variant for the background; never recolor or crop it.
- Verify new endpoint paths and parameters in the CoinGecko API docs first.
- Keep decision reasons in logs so a reader can understand every skip and trade.
