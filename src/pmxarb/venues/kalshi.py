"""Kalshi Trade API v2, public endpoints only (no key, no orders).

Docs: https://docs.kalshi.com. Prices arrive as dollar strings in *_dollars fields; the legacy
integer-cent fields are read as a fallback. Contract counts are fixed-point strings ("100.00").
"""
from __future__ import annotations

import logging
import time
from typing import Any

from ..config import KalshiCfg
from .base import BaseClient, HttpError

log = logging.getLogger(__name__)


def dollars(obj: dict, key: str) -> float | None:
    """Read `<key>_dollars` (string) or `<key>` (cents int) from a Kalshi object."""
    v = obj.get(f"{key}_dollars")
    if v not in (None, ""):
        return float(v)
    v = obj.get(key)
    if v in (None, ""):
        return None
    v = float(v)
    return v / 100.0 if v > 1.0 else v


def fixed(obj: dict, key: str) -> float:
    v = obj.get(f"{key}_fp")
    if v not in (None, ""):
        return float(v)
    v = obj.get(key)
    return float(v) if v not in (None, "") else 0.0


class KalshiClient(BaseClient):
    def __init__(self, cfg: KalshiCfg):
        super().__init__(cfg.base_url, cfg.max_rps, name="kalshi")
        self.cfg = cfg
        self._series_cache: dict[str, dict] = {}

    # ---- discovery -------------------------------------------------------------------------
    async def list_series(self, category: str | None = None) -> list[dict]:
        params: dict[str, Any] = {}
        if category:
            params["category"] = category
        data = await self.request("GET", "/series", params=params)
        return list((data or {}).get("series", []))

    async def get_series(self, ticker: str) -> dict:
        if ticker not in self._series_cache:
            data = await self.request("GET", f"/series/{ticker}")
            self._series_cache[ticker] = (data or {}).get("series", data or {})
        return self._series_cache[ticker]

    async def list_markets(self, series_ticker: str | None = None, status: str = "open",
                           event_ticker: str | None = None, max_pages: int = 50) -> list[dict]:
        out: list[dict] = []
        cursor: str | None = None
        for _ in range(max_pages):
            params: dict[str, Any] = {"limit": 1000, "status": status}
            if series_ticker:
                params["series_ticker"] = series_ticker
                params["mve_filter"] = "exclude"
            if event_ticker:
                params["event_ticker"] = event_ticker
            if cursor:
                params["cursor"] = cursor
            try:
                data = await self.request("GET", "/markets", params=params)
            except HttpError as exc:
                if exc.status == 400 and "mve_filter" in params:
                    params.pop("mve_filter")
                    data = await self.request("GET", "/markets", params=params)
                else:
                    raise
            page = (data or {}).get("markets", [])
            out.extend(page)
            cursor = (data or {}).get("cursor") or None
            if not cursor or not page:
                break
        return out

    async def list_events(self, series_ticker: str, status: str = "open", max_pages: int = 20) -> list[dict]:
        """Open events of a series with nested markets (event title carries the matchup text)."""
        out: list[dict] = []
        cursor: str | None = None
        for _ in range(max_pages):
            params: dict[str, Any] = {"limit": 200, "status": status, "series_ticker": series_ticker,
                                      "with_nested_markets": "true"}
            if cursor:
                params["cursor"] = cursor
            data = await self.request("GET", "/events", params=params)
            page = (data or {}).get("events", [])
            out.extend(page)
            cursor = (data or {}).get("cursor") or None
            if not cursor or not page:
                break
        return out

    async def get_market(self, ticker: str) -> dict:
        data = await self.request("GET", f"/markets/{ticker}")
        return (data or {}).get("market", data or {})

    async def get_event(self, event_ticker: str) -> dict:
        data = await self.request("GET", f"/events/{event_ticker}", params={"with_nested_markets": "false"})
        return (data or {}).get("event", data or {})

    # ---- books -----------------------------------------------------------------------------
    _tickers_repeated = False   # switched on if the API wants ?tickers=A&tickers=B instead of a comma list

    async def _orderbooks_chunk(self, chunk: list[str]) -> list[dict]:
        params: Any = [("tickers", t) for t in chunk] if self._tickers_repeated else {"tickers": ",".join(chunk)}
        data = await self.request("GET", "/markets/orderbooks", params=params)
        return list((data or {}).get("orderbooks", []))

    async def get_orderbooks(self, tickers: list[str]) -> dict[str, dict]:
        """Batch ladders (bids only, per Kalshi semantics). Returns ticker -> payload.
        The OpenAPI spec declares `tickers` as an exploded array; Kalshi's other endpoints take a
        comma list. We try the comma form and fall back to the repeated form once if it under-delivers."""
        out: dict[str, dict] = {}
        for i in range(0, len(tickers), self.cfg.orderbook_batch):
            chunk = tickers[i:i + self.cfg.orderbook_batch]
            try:
                books = await self._orderbooks_chunk(chunk)
            except HttpError as exc:
                if exc.status != 400 or self._tickers_repeated:
                    raise
                books = []
            if len(chunk) > 1 and len(books) <= 1 and not self._tickers_repeated:
                self._tickers_repeated = True
                alt = await self._orderbooks_chunk(chunk)
                if len(alt) > len(books):
                    log.info("kalshi orderbooks: switched to repeated tickers parameter")
                    books = alt
                else:
                    self._tickers_repeated = False
            for ob in books:
                out[ob.get("ticker", "")] = ob
        return out

    # ---- history ---------------------------------------------------------------------------
    async def get_candles(self, series_ticker: str, ticker: str, start_ts: int, end_ts: int,
                          period: int = 1, historical: bool = False) -> list[dict]:
        """1/60/1440-minute candles with yes_bid/yes_ask OHLC. Chunked to respect per-call limits."""
        out: list[dict] = []
        step = self.cfg.candle_batch_periods * period * 60
        t = start_ts
        while t < end_ts:
            t2 = min(end_ts, t + step)
            params = {"start_ts": t, "end_ts": t2, "period_interval": period}
            path = (f"/historical/markets/{ticker}/candlesticks" if historical
                    else f"/series/{series_ticker}/markets/{ticker}/candlesticks")
            try:
                data = await self.request("GET", path, params=params)
            except HttpError as exc:
                if exc.status == 404 and not historical:
                    return await self.get_candles(series_ticker, ticker, start_ts, end_ts, period, historical=True)
                raise
            out.extend((data or {}).get("candlesticks", []))
            t = t2
        return out

    @staticmethod
    def now() -> float:
        return time.time()
