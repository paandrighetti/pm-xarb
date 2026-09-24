"""Daily universe job: pull both venues, parse to canonical legs, join, persist pairs and the
diagnostics needed to see why something did not match."""
from __future__ import annotations

import logging
import re
import time
from typing import Any

from .config import Config
from .fees import FeeModel
from .matching.families import CanonLeg, kalshi_family, kalshi_sports, poly_family
from .matching.matcher import Overrides, build_pairs, save_pairs
from .matching.normalize import TeamBook
from .models import Pair
from .venues import HttpError, KalshiClient, PolymarketClient
from .venues.kalshi import fixed

log = logging.getLogger(__name__)


def _league_of(series_ticker: str, leagues: list[str]) -> str | None:
    for lg in leagues:
        if lg.upper() in series_ticker.upper():
            return lg
    return None


async def kalshi_legs(cfg: Config, kalshi: KalshiClient, teams: TeamBook, diag: dict[str, Any]) -> list[CanonLeg]:
    legs: list[CanonLeg] = []
    for family, tickers in cfg.venues.kalshi.series.items():
        for st in tickers:
            try:
                series = await kalshi.get_series(st)
            except HttpError as exc:
                diag.setdefault("kalshi_series_errors", []).append({"series": st, "error": str(exc)[:120]})
                log.warning("kalshi series %s unavailable: %s", st, exc)
                continue
            series = dict(series or {})
            series.setdefault("ticker", st)
            n_seen = n_parsed = 0
            try:
                if family == "sports":
                    league = _league_of(st, cfg.universe.leagues)
                    if not league:
                        continue
                    for ev in await kalshi.list_events(st):
                        n_seen += len(ev.get("markets") or [])
                        got = kalshi_sports(ev, series, league, teams)
                        n_parsed += len(got)
                        legs.extend(got)
                else:
                    for m in await kalshi.list_markets(series_ticker=st):
                        n_seen += 1
                        if fixed(m, "volume") < cfg.universe.min_kalshi_volume and fixed(m, "open_interest") <= 0:
                            continue
                        m.setdefault("series_ticker", st)
                        got = kalshi_family(family, m, series)
                        if got:
                            n_parsed += 1
                            legs.append(got)
            except HttpError as exc:
                diag.setdefault("kalshi_series_errors", []).append({"series": st, "error": str(exc)[:120]})
                log.warning("kalshi series %s listing failed: %s", st, exc)
                continue
            diag.setdefault("kalshi_series", []).append({"series": st, "family": family, "markets": n_seen, "parsed": n_parsed,
                                                          "fee_multiplier": series.get("fee_multiplier"),
                                                          "settlement": series.get("settlement_sources")})
    return legs


async def poly_legs(cfg: Config, poly: PolymarketClient, fees: FeeModel, teams: TeamBook, diag: dict[str, Any]) -> list[CanonLeg]:
    legs: list[CanonLeg] = []
    n_events = n_markets = n_kept = 0
    async for page in poly.iter_events():
        for ev in page:
            n_events += 1
            for m in ev.get("markets") or []:
                n_markets += 1
                if m.get("closed") or m.get("enableOrderBook") is False or m.get("acceptingOrders") is False:
                    continue
                if float(m.get("volume24hr") or 0.0) < cfg.universe.min_volume_24h_usd:
                    continue
                got = poly_family(ev, m, cfg.universe, fees, teams)
                if got:
                    n_kept += len(got)
                    legs.extend(got)
    diag["polymarket"] = {"events": n_events, "markets": n_markets, "legs": n_kept}
    return legs


async def build_universe(cfg: Config, kalshi: KalshiClient, poly: PolymarketClient, fees: FeeModel,
                         teams: TeamBook) -> tuple[list[Pair], dict[str, Any]]:
    t0 = time.time()
    diag: dict[str, Any] = {}
    k_legs = await kalshi_legs(cfg, kalshi, teams, diag)
    p_legs = await poly_legs(cfg, poly, fees, teams, diag)
    overrides = Overrides.load(cfg.universe.overrides_file)
    pairs, stats = build_pairs(k_legs, p_legs, cfg, overrides)
    matched_k = {p.legs["kalshi"].market_id for p in pairs}
    matched_p = {p.legs["polymarket"].leg_id for p in pairs}
    unmatched = {
        "kalshi": [{"key": l.key, "title": l.leg.title, "close_ts": l.leg.close_ts, "src": sorted(l.sources)}
                   for l in k_legs if l.leg.market_id not in matched_k][:400],
        "polymarket": [{"key": l.key, "title": l.leg.title, "close_ts": l.leg.close_ts, "src": sorted(l.sources)}
                       for l in p_legs if l.leg.leg_id not in matched_p][:400],
        "diag": diag,
    }
    stats["_total"]["seconds"] = round(time.time() - t0, 1)
    stats["_total"]["kalshi_legs"] = len(k_legs)
    stats["_total"]["polymarket_legs"] = len(p_legs)
    save_pairs(cfg, pairs, stats, unmatched)
    log.info("universe: %d pairs (%s) in %.0fs", len(pairs), summarize(stats), time.time() - t0)
    return pairs, stats


def summarize(stats: dict[str, Any]) -> str:
    parts = []
    for fam, d in stats.items():
        if fam.startswith("_"):
            continue
        parts.append(f"{fam}: {d.get('exact', 0)} exact / {d.get('basis', 0)} basis "
                     f"from {d.get('kalshi_legs', 0)}K x {d.get('polymarket_legs', 0)}P")
    return "; ".join(parts) or "nothing parsed"


def short_title(s: str, n: int = 70) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"
