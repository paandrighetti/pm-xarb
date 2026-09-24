"""Polymarket public endpoints: Gamma (discovery, metadata) and CLOB (books, price history,
market state). No signing, no orders.

Gamma serialises several list fields as JSON strings (outcomes, outcomePrices, clobTokenIds);
`jlist` decodes them. Timestamps in CLOB books are epoch milliseconds as strings.
"""
from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

from ..config import PolymarketCfg
from .base import BaseClient, HttpError

log = logging.getLogger(__name__)


def jlist(v: Any) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    try:
        out = json.loads(v)
        return out if isinstance(out, list) else []
    except (TypeError, ValueError):
        return []


class PolymarketClient(BaseClient):
    def __init__(self, cfg: PolymarketCfg):
        super().__init__(cfg.clob_url, cfg.max_rps, name="polymarket")
        self.cfg = cfg
        self.gamma = cfg.gamma_url

    # ---- discovery -------------------------------------------------------------------------
    async def iter_events(self, active: bool = True, closed: bool = False, max_pages: int = 300,
                          tag_slug: str | None = None) -> AsyncIterator[list[dict]]:
        """Pages of open events with nested markets and tags, via Gamma's keyset endpoint
        (GET /events/keyset: `limit` <= 500, `after_cursor` from the previous `next_cursor`;
        `offset` is refused with 422 beyond a shallow depth). Streamed so that a universe of
        several thousand events never sits in memory at once. `active` is applied client-side."""
        cursor: str | None = None
        ordered = True
        for _ in range(max_pages):
            params: dict[str, Any] = {"closed": str(closed).lower(), "limit": min(self.cfg.events_page, 500)}
            if ordered:
                params.update({"order": "volume24hr", "ascending": "false"})
            if tag_slug:
                params["tag_slug"] = tag_slug
            if cursor:
                params["after_cursor"] = cursor
            try:
                data = await self.request("GET", "/events/keyset", params=params, base=self.gamma)
            except HttpError as exc:
                if exc.status in (400, 422) and ordered:
                    ordered = False          # sort key rejected: paginate in the server's default order
                    continue
                raise
            page = list((data or {}).get("events", [])) if isinstance(data, dict) else list(data or [])
            if active:
                page = [e for e in page if e.get("active") is not False]
            if page:
                yield page
            cursor = (data or {}).get("next_cursor") if isinstance(data, dict) else None
            if not cursor:
                break

    async def list_events(self, **kw) -> list[dict]:
        out: list[dict] = []
        async for page in self.iter_events(**kw):
            out.extend(page)
        return out

    async def gamma_markets_by_condition(self, condition_ids: list[str]) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(condition_ids), 20):
            chunk = condition_ids[i:i + 20]
            params = [("condition_ids", c) for c in chunk]
            page = await self.request("GET", "/markets", params=params, base=self.gamma)
            out.extend(page or [])
        return out

    # ---- books -----------------------------------------------------------------------------
    async def get_books(self, token_ids: list[str]) -> dict[str, dict]:
        """Batch books. Returns token_id -> payload with bids/asks lists of {price,size}."""
        out: dict[str, dict] = {}
        for i in range(0, len(token_ids), self.cfg.book_batch):
            chunk = token_ids[i:i + self.cfg.book_batch]
            data = await self.request("POST", "/books", json=[{"token_id": t} for t in chunk])
            for book in data or []:
                out[str(book.get("asset_id", ""))] = book
        return out

    async def get_book(self, token_id: str) -> dict:
        return await self.request("GET", "/book", params={"token_id": token_id}) or {}

    # ---- state / resolution ----------------------------------------------------------------
    async def clob_market(self, condition_id: str) -> dict:
        try:
            return await self.request("GET", f"/markets/{condition_id}") or {}
        except HttpError as exc:
            if exc.status == 404:
                return {}
            raise

    # ---- history ---------------------------------------------------------------------------
    async def prices_history(self, token_id: str, start_ts: int, end_ts: int, fidelity_min: int = 1) -> list[dict]:
        """Price series for a token. Capped at ~720 points per response, so we chunk by
        fidelity * history_points and stitch."""
        out: list[dict] = []
        step = fidelity_min * 60 * self.cfg.history_points
        t = start_ts
        while t < end_ts:
            t2 = min(end_ts, t + step)
            params = {"market": token_id, "startTs": t, "endTs": t2, "fidelity": fidelity_min}
            data = await self.request("GET", "/prices-history", params=params)
            out.extend((data or {}).get("history", []))
            t = t2
        return out
