"""Scheduler for autopilot mode: named interval jobs, a heartbeat log, and a daily credit budget guard."""
import asyncio
import logging
import signal
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from .store import Store

log = logging.getLogger("core.autopilot")


@dataclass
class Job:
    """One named, interval-scheduled unit of autopilot work."""

    name: str
    interval_s: float
    run: Callable[[], Awaitable[None]]


def over_budget(credits_today: float, max_credits_per_day: float | None) -> bool:
    """True once today's credit usage has reached the daily budget."""
    return max_credits_per_day is not None and credits_today >= max_credits_per_day


class Autopilot:
    """Runs `jobs` forever on their own intervals, pausing all of them once the daily credit budget is hit."""

    def __init__(
        self,
        jobs: list[Job],
        store: Store,
        credits_today_fn: Callable[[], float],
        max_credits_per_day: float | None = None,
        heartbeat_s: float = 60,
    ):
        self.jobs = jobs
        self.store = store
        self.credits_today_fn = credits_today_fn
        self.max_credits_per_day = max_credits_per_day
        self.heartbeat_s = heartbeat_s
        self._stop = False

    def _over_budget(self) -> bool:
        return over_budget(self.credits_today_fn(), self.max_credits_per_day)

    async def _run_job(self, job: Job):
        last = self.store.get(f"last_run:{job.name}", 0)
        while not self._stop:
            if self._over_budget():
                log.info("budget reached, pausing %s", job.name)
                await asyncio.sleep(self.heartbeat_s)
                continue
            await asyncio.sleep(max(job.interval_s - (time.time() - last), 0))
            if self._stop or self._over_budget():
                if self._over_budget():
                    log.info("budget reached, pausing %s", job.name)
                continue
            try:
                await job.run()
            except Exception:
                # Every job here runs inside asyncio.gather(*tasks) in run_forever(), with no
                # return_exceptions=True: before this guard, one job's exception (a transient
                # network hiccup, an unexpected payload) propagated out of gather() and silently
                # killed every other job too -- the whole autopilot process would just stop doing
                # anything after that tick, with nothing in the logs pointing at why.
                log.exception("job %s raised; skipping this tick, will retry next interval", job.name)
            last = time.time()
            self.store.set(f"last_run:{job.name}", last)

    async def _heartbeat(self):
        while not self._stop:
            await asyncio.sleep(self.heartbeat_s)
            log.info("heartbeat: credits_today=%s jobs=%s", self.credits_today_fn(), [j.name for j in self.jobs])

    async def run_forever(self):
        """Runs every job plus the heartbeat until stop() is called or SIGINT/SIGTERM arrives."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop)
            except NotImplementedError:
                pass  # signal handlers aren't available on every platform (e.g. Windows)
        tasks = [asyncio.create_task(self._run_job(j)) for j in self.jobs]
        tasks.append(asyncio.create_task(self._heartbeat()))
        await asyncio.gather(*tasks)

    def stop(self):
        """Signals every job and the heartbeat to stop after their current sleep."""
        self._stop = True
