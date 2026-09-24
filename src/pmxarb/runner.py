"""Single-process supervisor: the poll loop plus a scheduler for universe, settlement sweep,
mark-to-market, daily report, weekly history screen and file compression. Stops cleanly on
SIGTERM so Docker restarts never lose the blotter or the paper state."""
from __future__ import annotations

import asyncio
import logging
import signal
import time
from datetime import datetime, timedelta, timezone

from . import telegram
from .config import Config
from .desk import Desk
from .fees import FeeModel
from .history import run_screen
from .matching.matcher import load_pairs
from .matching.normalize import TeamBook
from .report import Report
from .universe import build_universe, summarize
from .venues import KalshiClient, PolymarketClient

log = logging.getLogger(__name__)


def _next_utc(hour: int, weekday: int | None = None) -> float:
    """Next occurrence of hour:00 UTC (optionally on a given weekday, Monday=0), strictly in the future."""
    now = datetime.now(timezone.utc)
    t = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    while t <= now or (weekday is not None and t.weekday() != weekday):
        t += timedelta(days=1)
    return t.timestamp()


class Runner:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.kalshi = KalshiClient(cfg.venues.kalshi)
        self.poly = PolymarketClient(cfg.venues.polymarket)
        self.fees = FeeModel(cfg.venues.kalshi, cfg.venues.polymarket)
        self.teams = TeamBook.load(cfg.universe.teams_file)
        self.desk = Desk(cfg, self.kalshi, self.poly, self.fees)
        self.stop = asyncio.Event()

    async def refresh_universe(self) -> None:
        try:
            pairs, stats = await build_universe(self.cfg, self.kalshi, self.poly, self.fees, self.teams)
            self.desk.set_pairs(pairs)
            await telegram.send(f"pm-xarb universe: {len(pairs)} pairs\n{summarize(stats)}", self.cfg.telegram.enabled)
        except Exception as exc:  # noqa: BLE001 - the desk must keep polling on a failed rebuild
            log.exception("universe rebuild failed: %s", exc)
            await telegram.send(f"pm-xarb universe rebuild failed: {exc!r}"[:500], self.cfg.telegram.enabled)

    async def poll_loop(self) -> None:
        failures = 0
        while not self.stop.is_set():
            t0 = time.monotonic()
            try:
                await self.desk.poll()
                failures = 0
                for v in self.desk.drain_alerts():
                    await telegram.send(f"pm-xarb INVARIANT VIOLATION, execution halted, recording continues:\n{v}"[:500],
                                        self.cfg.telegram.enabled)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                log.exception("poll failed (%d): %s", failures, exc)
                await asyncio.sleep(min(60, 2 ** min(failures, 6)))
            delay = self.cfg.recorder.poll_seconds - (time.monotonic() - t0)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=max(0.05, delay))
            except asyncio.TimeoutError:
                pass

    async def scheduler(self) -> None:
        cfg = self.cfg
        next_universe = _next_utc(cfg.universe.refresh_utc_hour)
        next_report = _next_utc(cfg.report.utc_hour)
        next_mtm = _next_utc(cfg.paper.mtm_utc_hour)
        next_history = _next_utc(cfg.history.utc_hour, cfg.history.weekday) if cfg.history.enabled else float("inf")
        next_sweep = time.time() + 120
        next_gzip = time.time() + 600
        while not self.stop.is_set():
            now = time.time()
            try:
                if now >= next_sweep:
                    n = await self.desk.sweep_resolutions()
                    if n:
                        log.info("settled %d pairs", n)
                    next_sweep = now + cfg.resolution.sweep_minutes * 60
                if now >= next_gzip:
                    self.desk.compress()
                    next_gzip = now + 600
                if now >= next_mtm:
                    rec = self.desk.mark_to_market()
                    log.info("mtm %s", rec)
                    next_mtm = _next_utc(cfg.paper.mtm_utc_hour)
                if now >= next_report:
                    path, digest = Report(cfg).write()
                    log.info("report written %s", path)
                    await telegram.send(digest, cfg.telegram.enabled)
                    next_report = _next_utc(cfg.report.utc_hour)
                if now >= next_universe:
                    await self.refresh_universe()
                    next_universe = _next_utc(cfg.universe.refresh_utc_hour)
                if now >= next_history:
                    text, rows = await run_screen(cfg, self.kalshi, self.poly, self.fees, list(self.desk.active.values()))
                    await telegram.send(f"pm-xarb history screen (upper bound): {len(rows)} pairs screened; see data/history/", cfg.telegram.enabled)
                    next_history = _next_utc(cfg.history.utc_hour, cfg.history.weekday)
            except Exception as exc:  # noqa: BLE001
                log.exception("scheduled job failed: %s", exc)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=20)
            except asyncio.TimeoutError:
                pass

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop.set)
            except NotImplementedError:
                pass
        pairs, gen_ts = load_pairs(self.cfg)
        if pairs and time.time() - gen_ts < 26 * 3600:
            self.desk.set_pairs(pairs)
        else:
            await self.refresh_universe()
        await telegram.send(f"pm-xarb started: {len(self.desk.active)} pairs, poll {self.cfg.recorder.poll_seconds}s, "
                            f"cash {self.desk.paper.cash}", self.cfg.telegram.enabled)
        tasks = [asyncio.create_task(self.poll_loop()), asyncio.create_task(self.scheduler())]
        await self.stop.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.desk.close()
        await self.kalshi.aclose()
        await self.poly.aclose()
        log.info("stopped cleanly")
