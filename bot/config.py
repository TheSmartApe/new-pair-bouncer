"""Bot-level tunables. Strategy YAML owns filters/exits; this owns operational defaults."""

DEFAULTS = {
    "interval_s": 60,  # seconds between scans
    "max_candidates": 20,  # candidates pulled from the source per scan
    "max_token_info_calls": 15,  # token_info costs 1 credit each; caps a scan's spend
    "max_smart_money_calls": 8,  # top_traders costs 1 credit each
    "max_credits_per_day": 5000,  # autopilot pauses once today's credits reach this
    "heartbeat_s": 60,
    "price_poll_s": 20,  # how often open positions are repriced when not on WebSocket
    "use_websocket": True,  # only takes effect when the plan has it (probe_capabilities)
}

SCANS_DIR = "data/scans"
RECORDINGS_DIR = "data/recordings"
RUNS_DIR = "runs"
STRATEGIES_DIR = "strategies"
