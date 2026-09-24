"""Venue fee curves. Both venues charge takers a quadratic fee in the contract price,
rate * P * (1 - P) per contract; the difference is the rate, the rounding and who pays maker fees.

Kalshi, fee schedule 7 July 2026: fee = round_up(M * 0.07 * C * P * (1-P)), rounded so that
fee + cost lands on a centicent; M is a per-series multiplier (default 1) exposed by
GET /series/{ticker}.fee_multiplier. Maker fee uses 0.0175 where the series fee_type says so.

Polymarket, 2026 schedule: fee = C * rate(category) * P * (1-P), makers pay 0.
"""
from __future__ import annotations

import math

from .config import KalshiCfg, PolymarketCfg
from .models import KALSHI, Leg


def _round_up(x: float, step: float) -> float:
    if step <= 0:
        return x
    return math.ceil(round(x / step, 9)) * step


class FeeModel:
    def __init__(self, kalshi: KalshiCfg, poly: PolymarketCfg):
        self.k = kalshi
        self.p = poly

    def taker_rate(self, leg: Leg) -> float:
        if leg.venue == KALSHI:
            return self.k.taker_rate * (leg.fee_multiplier or self.k.default_fee_multiplier)
        return self.p.taker_rates.get(leg.fee_category, self.p.taker_rates.get("other", 0.05))

    def marginal_fee(self, leg: Leg, price: float) -> float:
        """Fee per contract at a given price, before order-level rounding. Used inside the book walk."""
        return self.taker_rate(leg) * price * (1.0 - price)

    def order_fee(self, leg: Leg, price: float, qty: float) -> float:
        """Fee on a whole order (qty contracts at an average price), with the venue's rounding."""
        raw = self.taker_rate(leg) * qty * price * (1.0 - price)
        if leg.venue == KALSHI:
            return _round_up(raw, self.k.fee_rounding)
        return raw

    def poly_category(self, tag_slugs: list[str]) -> str:
        for slug in tag_slugs:
            cat = self.p.tag_categories.get(slug.lower())
            if cat:
                return cat
        return "other"
