STRATEGY ?= new-launch-sniff
MINUTES ?= 5
INTERVAL ?=
MAX_CREDITS ?=
RUN ?= latest
HANDLE ?=
FILE ?=
OUT ?= docs/screens/dashboard.png
NO_WEBSOCKET ?=

.PHONY: install run test backtest forward autopilot report article-kit set-link record replay screenshot sniper-collect sniper-autopilot sniper-leaderboard sniper-report sniper-profile sniper-enrich

install:
	uv venv --python 3.12 .venv
	uv pip install -e .[dev,article-kit]

test:
	.venv/bin/pytest -q

# One scan/trade loop drives both `forward` (bounded, --minutes) and `autopilot` (unbounded, restart-safe).
run:
	.venv/bin/python -m bot run --strategy $(STRATEGY) $(if $(INTERVAL),--interval $(INTERVAL)) $(if $(MAX_CREDITS),--max-credits $(MAX_CREDITS)) $(if $(NO_WEBSOCKET),--no-websocket)

forward:
	.venv/bin/python -m bot run --strategy $(STRATEGY) --minutes $(MINUTES) $(if $(INTERVAL),--interval $(INTERVAL)) $(if $(MAX_CREDITS),--max-credits $(MAX_CREDITS)) $(if $(NO_WEBSOCKET),--no-websocket)

autopilot:
	.venv/bin/python -m bot run --strategy $(STRATEGY) $(if $(INTERVAL),--interval $(INTERVAL)) $(if $(MAX_CREDITS),--max-credits $(MAX_CREDITS)) $(if $(NO_WEBSOCKET),--no-websocket)

backtest:
	.venv/bin/python -m bot backtest --strategy $(STRATEGY) $(ARGS)

report:
	.venv/bin/python -m bot report --run $(RUN)

article-kit:
	.venv/bin/python -m bot article-kit --run $(RUN) --handle $(HANDLE) --dashboard-screenshot

set-link:
	.venv/bin/python -m bot set-link --handle $(HANDLE)

record:
	.venv/bin/python -m bot record --strategy $(STRATEGY)

replay:
	.venv/bin/python -m bot replay --file $(FILE)

screenshot:
	.venv/bin/python -m bot screenshot --run $(RUN) --out $(OUT)

# ---- Serial Sniper Tracker ----
SNIPER_MINUTES ?=

sniper-collect:
	.venv/bin/python -m sniper collect $(if $(SNIPER_MINUTES),--minutes $(SNIPER_MINUTES))

sniper-autopilot:
	mkdir -p data && nohup .venv/bin/python -m sniper collect >> data/collect.log 2>&1 &

sniper-leaderboard:
	.venv/bin/python -m sniper leaderboard

sniper-report:
	.venv/bin/python -m sniper report $(if $(HANDLE),--handle $(HANDLE))

sniper-profile:
	.venv/bin/python -m sniper profile --top 20

sniper-enrich:
	.venv/bin/python -m sniper enrich
