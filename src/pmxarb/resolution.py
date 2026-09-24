"""Settlement values read from each venue, never inferred from the model or from the other venue."""
from __future__ import annotations

import logging

from .models import KALSHI, POLYMARKET, Leg
from .venues import HttpError, KalshiClient, PolymarketClient
from .venues.kalshi import dollars

log = logging.getLogger(__name__)

KALSHI_DONE = {"settled", "finalized", "determined"}


async def kalshi_yes_value(kalshi: KalshiClient, leg: Leg) -> float | None:
    try:
        m = await kalshi.get_market(leg.market_id)
    except HttpError as exc:
        log.warning("kalshi status %s: %s", leg.market_id, exc)
        return None
    status = str(m.get("status") or "").lower()
    result = str(m.get("result") or "").lower()
    v: float | None = None
    if result in ("yes", "no"):
        v = 1.0 if result == "yes" else 0.0
    elif status in KALSHI_DONE:
        sv = dollars(m, "settlement_value")
        v = sv if sv is not None else None
    if v is None:
        return None
    return 1.0 - v if leg.yes_is_venue_no else v


async def polymarket_yes_value(poly: PolymarketClient, leg: Leg) -> float | None:
    try:
        m = await poly.clob_market(leg.market_id)
    except HttpError as exc:
        log.warning("polymarket status %s: %s", leg.market_id, exc)
        return None
    if not m or not m.get("closed"):
        return None
    tokens = m.get("tokens") or []
    winners = [t for t in tokens if t.get("winner")]
    yes = next((t for t in tokens if str(t.get("token_id")) == str(leg.yes_token)), None)
    if winners:
        return 1.0 if any(str(t.get("token_id")) == str(leg.yes_token) for t in winners) else 0.0
    if yes is not None and yes.get("price") is not None:
        p = float(yes["price"])
        if abs(p - 0.5) < 1e-6:      # 50/50 resolution (voided event)
            return 0.5
        if p <= 0.02 or p >= 0.98:
            return float(round(p))
    return None


async def yes_values(kalshi: KalshiClient, poly: PolymarketClient, legs: dict[str, Leg]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    if KALSHI in legs:
        out[KALSHI] = await kalshi_yes_value(kalshi, legs[KALSHI])
    if POLYMARKET in legs:
        out[POLYMARKET] = await polymarket_yes_value(poly, legs[POLYMARKET])
    return out
