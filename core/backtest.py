"""Event-replay backtest runner with a no-lookahead guard, and a walk-forward train/test split."""
from dataclasses import dataclass
from typing import Any


@dataclass
class Event:
    """One chronologically-ordered thing that happened: a pool trade, a candle close, a wallet trade."""

    ts: float
    kind: str
    data: Any


class Context:
    """A view over `events` that only ever exposes data with ts <= `now`, advanced by run() as it walks forward."""

    def __init__(self, events: list[Event]):
        self._events = events
        self.now = float("-inf")

    def history(self, kind: str | None = None) -> list[Event]:
        """Every event up to and including `now`, oldest first. Never includes anything from the future."""
        return [e for e in self._events if e.ts <= self.now and (kind is None or e.kind == kind)]


def run(events: list[Event], strategy) -> Context:
    """Feeds `events` to strategy.on_event(event, ctx) in timestamp order."""
    ordered = sorted(events, key=lambda e: e.ts)
    ctx = Context(ordered)
    for event in ordered:
        ctx.now = event.ts
        strategy.on_event(event, ctx)
    return ctx


def walk_forward_split(events: list[Event], train_frac: float = 0.6) -> tuple[list[Event], list[Event]]:
    """Splits chronologically-sorted events into an earlier "select on" slice and a later "test on" slice."""
    ordered = sorted(events, key=lambda e: e.ts)
    cut = round(len(ordered) * train_frac)
    return ordered[:cut], ordered[cut:]
