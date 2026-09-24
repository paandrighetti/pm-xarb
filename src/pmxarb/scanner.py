"""Opportunity detection on a synchronized snapshot, plus episode tracking (how long an
opportunity survives is as informative as its size)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .books import walk_pair
from .config import ScannerCfg
from .fees import FeeModel
from .models import KALSHI, POLYMARKET, Book, Combo, Opportunity, Pair

COMBOS = [Combo(KALSHI, "yes", POLYMARKET, "no"), Combo(KALSHI, "no", POLYMARKET, "yes")]


@dataclass
class Episode:
    pair_id: str
    family: str
    klass: str
    combo: str
    first_ts: float
    last_ts: float
    polls: int = 1
    max_edge: float = 0.0
    max_qty: int = 0
    max_edge_total: float = 0.0
    misses: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"pair_id": self.pair_id, "family": self.family, "klass": self.klass, "combo": self.combo,
                "first_ts": self.first_ts, "last_ts": self.last_ts, "lifetime_s": round(self.last_ts - self.first_ts, 3),
                "polls": self.polls, "max_edge": round(self.max_edge, 5), "max_qty": self.max_qty,
                "max_edge_total": round(self.max_edge_total, 4)}


class Scanner:
    def __init__(self, cfg: ScannerCfg, fees: FeeModel, max_notional: float):
        self.cfg = cfg
        self.fees = fees
        self.max_notional = max_notional
        self.episodes: dict[tuple[str, str], Episode] = {}

    def scan_pair(self, pair: Pair, books: dict[str, Book], ts: float) -> list[Opportunity]:
        out: list[Opportunity] = []
        ka, pa = books.get(KALSHI), books.get(POLYMARKET)
        if not ka or not pa:
            return out
        leg_k, leg_p = pair.legs[KALSHI], pair.legs[POLYMARKET]
        fee_k = lambda p: self.fees.marginal_fee(leg_k, p)  # noqa: E731
        fee_p = lambda p: self.fees.marginal_fee(leg_p, p)  # noqa: E731
        days_locked = max(0.0, (pair.close_ts - ts) / 86400.0)
        for combo in COMBOS:
            book_a, book_b = books[combo.venue_a], books[combo.venue_b]
            asks_a, asks_b = book_a.asks(combo.side_a), book_b.asks(combo.side_b)
            w = walk_pair(asks_a, asks_b, fee_k if combo.venue_a == KALSHI else fee_p,
                          fee_p if combo.venue_b == POLYMARKET else fee_k,
                          self.cfg.min_edge, self.max_notional, self.cfg.min_level_size)
            if not w or w.qty < self.cfg.min_qty:
                continue
            out.append(Opportunity(
                ts=ts, pair_id=pair.pair_id, family=pair.family, klass=pair.klass, combo=combo.name, qty=w.qty,
                cost_a=w.cost_a, cost_b=w.cost_b, fees_a=w.fees_a, fees_b=w.fees_b,
                edge_per_contract=w.marginal_edge, edge_total=w.edge_total,
                best_a=asks_a[0].price if asks_a else 0.0, best_b=asks_b[0].price if asks_b else 0.0,
                limit_a=w.limit_a, limit_b=w.limit_b, days_locked=days_locked))
        return out

    def track(self, opps: list[Opportunity], ts: float) -> list[dict[str, Any]]:
        """Update episodes; return the episodes that just ended (absent for two polls)."""
        seen: set[tuple[str, str]] = set()
        for o in opps:
            k = (o.pair_id, o.combo)
            seen.add(k)
            ep = self.episodes.get(k)
            if ep is None:
                self.episodes[k] = Episode(o.pair_id, o.family, o.klass, o.combo, ts, ts, 1,
                                           o.edge_per_contract, int(o.qty), o.edge_total)
            else:
                ep.last_ts, ep.polls, ep.misses = ts, ep.polls + 1, 0
                ep.max_edge = max(ep.max_edge, o.edge_per_contract)
                ep.max_qty = max(ep.max_qty, int(o.qty))
                ep.max_edge_total = max(ep.max_edge_total, o.edge_total)
        ended: list[dict[str, Any]] = []
        for k, ep in list(self.episodes.items()):
            if k in seen:
                continue
            ep.misses += 1
            if ep.misses >= 2:
                ended.append(ep.to_dict())
                del self.episodes[k]
        return ended

    def flush(self) -> list[dict[str, Any]]:
        out = [ep.to_dict() for ep in self.episodes.values()]
        self.episodes.clear()
        return out
