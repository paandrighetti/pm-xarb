"""Book normalization (venue payload -> canonical two-sided Book) and book-walking arithmetic.

Kalshi returns bids only, for YES and NO. A YES bid at p is the same resting order as a NO ask at
1 - p with the same size, so the executable YES ask ladder is the mirror of the NO bid ladder.
Polymarket returns bids and asks per outcome token; we read both tokens and take each side's
own ladder rather than assuming the operator has merged complementary liquidity.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Callable, Iterable

from .models import Book, Leg, Level

FeeFn = Callable[[float], float]


def _levels(raw: Iterable, desc: bool) -> list[Level]:
    out: list[Level] = []
    for item in raw or []:
        if isinstance(item, dict):
            p, s = float(item["price"]), float(item["size"])
        else:
            p, s = float(item[0]), float(item[1])
            if p > 1.0:          # legacy cents payload
                p = p / 100.0
        if s > 0:
            out.append(Level(p, s))
    out.sort(key=lambda l: l.price, reverse=desc)
    return out


def _mirror(levels: list[Level]) -> list[Level]:
    return [Level(round(1.0 - l.price, 6), l.size) for l in levels]


def kalshi_book(leg: Leg, payload: dict, ts_recv: float | None = None) -> Book:
    ob = payload.get("orderbook_fp") or {}
    yes_raw = ob.get("yes_dollars")
    no_raw = ob.get("no_dollars")
    if yes_raw is None and no_raw is None:            # legacy shape, prices in cents
        legacy = payload.get("orderbook") or {}
        yes_raw, no_raw = legacy.get("yes"), legacy.get("no")
    v_yes_bids = _levels(yes_raw, desc=True)
    v_no_bids = _levels(no_raw, desc=True)
    v_yes_asks = _mirror(v_no_bids)                   # already ascending since no_bids descending
    v_no_asks = _mirror(v_yes_bids)
    if leg.yes_is_venue_no:
        v_yes_bids, v_no_bids = v_no_bids, v_yes_bids
        v_yes_asks, v_no_asks = v_no_asks, v_yes_asks
    return Book(venue=leg.venue, leg_id=leg.leg_id, ts_recv=ts_recv or time.time(), ts_src=None,
                yes_bids=v_yes_bids, yes_asks=v_yes_asks, no_bids=v_no_bids, no_asks=v_no_asks)


def polymarket_book(leg: Leg, yes_payload: dict | None, no_payload: dict | None,
                    ts_recv: float | None = None) -> Book:
    def ts_of(p):
        try:
            return float(p.get("timestamp")) / 1000.0 if p and p.get("timestamp") else None
        except (TypeError, ValueError):
            return None

    yes_bids = _levels((yes_payload or {}).get("bids"), desc=True)
    yes_asks = _levels((yes_payload or {}).get("asks"), desc=False)
    if no_payload:
        no_bids = _levels(no_payload.get("bids"), desc=True)
        no_asks = _levels(no_payload.get("asks"), desc=False)
    else:                                              # derive by complement when the NO book is missing
        no_bids = _mirror(yes_asks)
        no_asks = _mirror(yes_bids)
    ts_src = ts_of(yes_payload) or ts_of(no_payload)
    return Book(venue=leg.venue, leg_id=leg.leg_id, ts_recv=ts_recv or time.time(), ts_src=ts_src,
                yes_bids=yes_bids, yes_asks=yes_asks, no_bids=no_bids, no_asks=no_asks)


@dataclass
class Walk:
    qty: int
    cost_a: float
    cost_b: float
    fees_a: float
    fees_b: float
    edge_total: float
    marginal_edge: float     # net edge of the last contract taken
    limit_a: float
    limit_b: float

    @property
    def best_edge(self) -> float:
        return self.marginal_edge if self.qty else 0.0


def walk_pair(asks_a: list[Level], asks_b: list[Level], fee_a: FeeFn, fee_b: FeeFn,
              min_edge: float, max_notional: float, min_level_size: float = 0.0,
              max_qty: int | None = None) -> Walk | None:
    """Walk two ask ladders simultaneously, taking contracts while the combined cost of one
    contract on each leg, fees included, stays below 1 - min_edge and the combined notional
    stays under max_notional (and the count under max_qty). Quantities are whole contracts.

    This is the only place that decides how many contracts a hedge is worth taking. The scanner
    calls it on the detection snapshot to size an opportunity; the executor calls it again on the
    fill snapshot so that a fill can never cost more than the pair pays."""
    a = [l for l in asks_a if l.size >= min_level_size]
    b = [l for l in asks_b if l.size >= min_level_size]
    if not a or not b:
        return None
    i = j = 0
    rem_a, rem_b = math.floor(a[0].size), math.floor(b[0].size)
    qty = 0
    cost_a = cost_b = fees_a = fees_b = edge_total = 0.0
    notional = 0.0
    last_edge = 0.0
    limit_a, limit_b = a[0].price, b[0].price
    while i < len(a) and j < len(b):
        pa, pb = a[i].price, b[j].price
        fa, fb = fee_a(pa), fee_b(pb)
        per_contract = pa + fa + pb + fb
        edge = 1.0 - per_contract
        if edge < min_edge:
            break
        budget = math.floor((max_notional - notional) / per_contract) if per_contract > 0 else 0
        if max_qty is not None:
            budget = min(budget, max_qty - qty)
        take = min(rem_a, rem_b, budget)
        if take <= 0:
            break
        qty += take
        cost_a += take * pa
        cost_b += take * pb
        fees_a += take * fa
        fees_b += take * fb
        edge_total += take * edge
        notional += take * per_contract
        last_edge = edge
        limit_a, limit_b = pa, pb
        rem_a -= take
        rem_b -= take
        if rem_a <= 0:
            i += 1
            rem_a = math.floor(a[i].size) if i < len(a) else 0
        if rem_b <= 0:
            j += 1
            rem_b = math.floor(b[j].size) if j < len(b) else 0
        if budget - take <= 0:
            break
    if qty == 0:
        return None
    return Walk(qty=qty, cost_a=cost_a, cost_b=cost_b, fees_a=fees_a, fees_b=fees_b,
                edge_total=edge_total, marginal_edge=last_edge, limit_a=limit_a, limit_b=limit_b)


@dataclass
class Exec:
    qty: int
    notional: float
    fees: float

    @property
    def vwap(self) -> float:
        return self.notional / self.qty if self.qty else 0.0


def walk_buy(asks: list[Level], fee: FeeFn, qty: int, limit: float) -> Exec:
    """Fill up to qty against ask levels priced at or below limit."""
    filled, notional, fees = 0, 0.0, 0.0
    for lv in asks:
        if lv.price > limit + 1e-9 or filled >= qty:
            break
        take = min(qty - filled, math.floor(lv.size))
        if take <= 0:
            continue
        filled += take
        notional += take * lv.price
        fees += take * fee(lv.price)
    return Exec(filled, notional, fees)


def walk_sell(bids: list[Level], fee: FeeFn, qty: int) -> Exec:
    """Sell up to qty against bid levels at market (used to unwind a naked leg)."""
    filled, proceeds, fees = 0, 0.0, 0.0
    for lv in bids:
        if filled >= qty:
            break
        take = min(qty - filled, math.floor(lv.size))
        if take <= 0:
            continue
        filled += take
        proceeds += take * lv.price
        fees += take * fee(lv.price)
    return Exec(filled, proceeds, fees)
