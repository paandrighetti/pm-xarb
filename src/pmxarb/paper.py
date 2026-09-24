"""Paper execution with leg risk and capital accounting.

Timeline for one opportunity seen on poll n (snapshot S_n), model `joint`:
  poll n   : intent created; it carries the size and the edge the detection walked
  poll n+1 : both ladders are walked again on S_{n+1}; what is still jointly profitable (keeping
             edge_retention of the seen edge) fills on both legs, the rest is left alone
Model `sequenced`:
  poll n+1 : the first leg fills on S_{n+1} within the part of the edge not reserved
  poll n+2 : the second leg fills on S_{n+2} with the limit that leaves the hedge its required edge
  poll n+3 : any unhedged excess is sold at market against S_{n+3} bids (on_leg_failure: unwind)
One poll interval therefore plays the role of round-trip latency. This is pessimistic for a
co-located taker and optimistic about nothing else: no queue position, no hidden size.

The price limit per leg that earlier versions used is gone. It let a leg fill up to a fixed number
of cents above the seen level whatever the edge was, so with a 0.2 cent minimum edge and 1 cent of
tolerance per leg the majority of admissible fills were above par: the desk was measuring adverse
selection, not arbitrage. The constraint now lives where the payoff lives, on the pair.

Capital is per venue and cannot move between venues. Bought contracts lock cash until the venue
reports a settlement; payouts come from the venue's own outcome, so basis pairs can pay 0 or 2.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict
from typing import Any

from .books import walk_buy, walk_pair, walk_sell
from .config import PaperCfg
from .fees import FeeModel
from .models import KALSHI, POLYMARKET, Book, Fill, Intent, Opportunity, Pair, Position
from .recorder import Blotter

UNWIND_MAX_POLLS = 10
NO_BOOK_MAX_POLLS = 3


def _combo_parts(combo: str) -> tuple[str, str, str, str]:
    a, b = combo.split("+")
    va, sa = a.split(":")
    vb, sb = b.split(":")
    return va, sa, vb, sb


class PaperDesk:
    def __init__(self, cfg: PaperCfg, fees: FeeModel, blotter: Blotter):
        self.cfg = cfg
        self.fees = fees
        self.blotter = blotter
        self.cash: dict[str, float] = {KALSHI: cfg.capital_per_venue_usd, POLYMARKET: cfg.capital_per_venue_usd}
        self.positions: dict[str, Position] = {}          # key pair|venue|side
        self.intents: dict[str, Intent] = {}              # pending or unwinding
        self.unwind: dict[str, dict[str, Any]] = {}       # intent_id -> {venue, side, qty, polls}
        self.cooldown: dict[str, int] = {}                # pair_id -> poll index until which we stay out
        self.settled: dict[str, dict[str, float]] = {}    # pair_id -> venue -> settlement value of canonical YES
        self.escrow: dict[str, dict[str, float]] = {}     # pair_id -> {pnl, since}: result of a half-settled pair
        self.realized: float = 0.0
        self.stats: dict[str, int] = {"intents": 0, "filled": 0, "partial": 0, "missed": 0, "unwound": 0, "stuck": 0}
        # Circuit breaker. Set by the desk when an invariant fails; new intents stop, the recorder,
        # the scanner, the sweep and the report go on. Re-evaluated every poll, so it lifts on its
        # own only if the books become consistent again, which a real violation never does.
        self.halted: str | None = None

    # ---- helpers ---------------------------------------------------------------------------
    @staticmethod
    def _pkey(pair_id: str, venue: str, side: str) -> str:
        return f"{pair_id}|{venue}|{side}"

    def exposure(self, pair_id: str) -> float:
        locked = sum(p.cost + p.fees for k, p in self.positions.items() if k.startswith(pair_id + "|") and not p.resolved)
        pending = sum(i.qty * (i.limit_a + i.limit_b) for i in self.intents.values()
                      if i.pair_id == pair_id and i.status == "pending")
        return locked + pending

    def event_exposure(self, event_key: str, pairs: dict[str, Pair]) -> float:
        """Locked plus pending notional over every pair that hedges the same underlying event."""
        same = {pid for pid, p in pairs.items() if p.event_key == event_key}
        return sum(self.exposure(pid) for pid in same)

    def open_pairs(self) -> set[str]:
        return {k.split("|")[0] for k, p in self.positions.items() if not p.resolved and p.qty > 0}

    def busy_events(self, pairs: dict[str, Pair]) -> set[str]:
        """Events with a hedge on, an intent in flight, or a verdict already known on any of their
        pairs. A pair that has started to settle is over: whatever its books still show is stale,
        and buying into it would put contracts on a position the settlement has already closed."""
        busy = (self.open_pairs() | {i.pair_id for i in self.intents.values() if i.status in ("pending", "unwinding")}
                | set(self.settled) | {k.split("|")[0] for k, p in self.positions.items() if p.resolved})
        return {pairs[pid].event_key for pid in busy if pid in pairs}

    def class_exposure(self, pairs: dict[str, Pair]) -> dict[str, float]:
        """Notional locked per resolution class, live intents included."""
        used: dict[str, float] = {}
        for k, p in self.positions.items():
            if p.resolved or p.qty <= 0:
                continue
            pair = pairs.get(k.split("|")[0])
            if pair is not None:
                used[pair.klass] = used.get(pair.klass, 0.0) + p.cost + p.fees
        for i in self.intents.values():
            if i.status == "pending":
                used[i.klass] = used.get(i.klass, 0.0) + i.qty * (i.limit_a + i.limit_b)
        return used

    def _fee_fn(self, pair: Pair, venue: str):
        leg = pair.legs[venue]
        return lambda p: self.fees.marginal_fee(leg, p)

    # ---- invariants --------------------------------------------------------------------------
    def locked(self) -> float:
        return sum(p.cost + p.fees for p in self.positions.values() if not p.resolved and p.qty > 0)

    def invariants(self, pairs: dict[str, Pair]) -> list[str]:
        """Every dollar that left cash is locked in an open position, booked in realized, or parked
        in escrow; nothing else is allowed to hold money. Checked after every poll. The first
        weekend broke this twice without any test noticing, because every test was written from
        the same model as the code; this check is orthogonal to the model."""
        out: list[str] = []
        capital = 2 * self.cfg.capital_per_venue_usd
        escrow = sum(e["pnl"] for e in self.escrow.values())
        gap = sum(self.cash.values()) - (capital + self.realized + escrow - self.locked())
        if abs(gap) > 0.01:
            out.append(f"cash identity off by {gap:+.2f}")
        zombies = [k for k, p in self.positions.items() if p.qty <= 0 and not p.resolved and not p.voided and p.fees > 1e-9]
        if zombies:
            out.append(f"{len(zombies)} zero-quantity positions still carry fees")
        for v, c in self.cash.items():
            if c < -0.01:
                out.append(f"negative cash on {v}: {c:.2f}")
        bad = [k for k, p in self.positions.items() if p.qty < -1e-9 or p.cost < -0.01 or p.fees < -1e-6]
        if bad:
            out.append(f"negative quantity, cost or fees on {bad[:3]}")
        missing = self.open_pairs() - set(pairs)
        if missing:
            out.append(f"open positions without a pair definition: {sorted(missing)[:3]}")
        esc_missing = set(self.escrow) - set(pairs)
        if esc_missing:
            out.append(f"escrow without a pair definition: {sorted(esc_missing)[:3]}")
        for iid, i in self.intents.items():
            if i.status == "unwinding" and iid not in self.unwind:
                out.append(f"intent {iid} is unwinding with no unwind order")
        return out

    # ---- decisions -------------------------------------------------------------------------
    def consider(self, poll: int, ts: float, opps: list[Opportunity], pairs: dict[str, Pair], cutoff_ts: float) -> list[Intent]:
        created: list[Intent] = []
        if self.halted:
            return created
        class_used = self.class_exposure(pairs)
        busy = self.busy_events(pairs)
        for o in sorted(opps, key=lambda x: -x.edge_total):
            pair = pairs.get(o.pair_id)
            if pair is None or self.cooldown.get(o.pair_id, -1) >= poll:
                continue
            if pair.klass not in self.cfg.execute_classes:
                continue
            # One hedge per event at a time. A game gives two pairs (one per team) that buy the same
            # Polymarket token and equivalent Kalshi exposure; the first weekend traded both within
            # the same second, doubling size and counting one leg failure twice. Without this rule
            # the desk also stacks opposite hedges on a pair as the market moves through a threshold:
            # not wrong, but the position stops being one experiment with one outcome.
            if pair.event_key in busy:
                continue
            va, sa, vb, sb = _combo_parts(o.combo)
            # The fill is governed by the pair's budget (it must keep edge_retention of the edge
            # seen), not by a price limit per leg, so a hedge costs at most 1 per contract: reserve
            # at par. A single leg can pay at most its seen level plus the whole edge.
            per_contract = 1.0
            cap = self.cfg.max_notional_per_class_usd.get(pair.klass)
            room = self.cfg.max_notional_per_pair_usd - self.event_exposure(pair.event_key, pairs)
            if cap is not None:
                room = min(room, cap - class_used.get(pair.klass, 0.0))
            qty = int(min(o.qty, room // per_contract,
                          self.cash[va] // max(0.01, o.limit_a + o.edge_per_contract),
                          self.cash[vb] // max(0.01, o.limit_b + o.edge_per_contract)))
            if qty <= 0:
                continue
            class_used[pair.klass] = class_used.get(pair.klass, 0.0) + qty * per_contract
            intent = Intent(intent_id=uuid.uuid4().hex[:12], created_ts=ts, created_poll=poll, pair_id=o.pair_id,
                            family=o.family, klass=o.klass, combo=o.combo, venue_a=va, side_a=sa, venue_b=vb, side_b=sb,
                            qty=qty, limit_a=o.limit_a, limit_b=o.limit_b, edge_seen=o.edge_per_contract,
                            data_cutoff_ts=cutoff_ts, model=self.cfg.execution_model)
            self.intents[intent.intent_id] = intent
            self.stats["intents"] += 1
            busy.add(pair.event_key)
            rec = asdict(intent)
            rec["event_key"] = pair.event_key
            self.blotter.append("intents", rec)
            created.append(intent)
        return created

    # ---- execution against the next snapshot -------------------------------------------------
    def on_snapshot(self, poll: int, ts: float, books: dict[str, dict[str, Book]], pairs: dict[str, Pair]) -> None:
        """books: pair_id -> venue -> Book for this poll."""
        for iid, intent in list(self.intents.items()):
            if intent.status == "pending" and poll > intent.created_poll:
                self._fill(intent, ts, books.get(intent.pair_id, {}), pairs.get(intent.pair_id), poll)
            elif intent.status == "unwinding":
                self._unwind(intent, ts, books.get(intent.pair_id, {}), pairs.get(intent.pair_id), poll)

    def _buy(self, intent: Intent, pair: Pair, venue: str, side: str, book: Book | None, limit: float, ts: float,
             qty: int | None = None) -> tuple[int, float, float]:
        if book is None or pair is None:
            return 0, 0.0, 0.0
        pos = self.positions.get(self._pkey(pair.pair_id, venue, side))
        if pos is not None and (pos.resolved or pos.voided):
            # The venue has already paid this position out (or it was voided). Contracts added to it
            # would sit outside both locked and realized for ever. The market is over: no fill.
            return 0, 0.0, 0.0
        ex = walk_buy(book.asks(side), self._fee_fn(pair, venue), int(intent.qty if qty is None else qty), limit)
        if ex.qty <= 0:
            return 0, 0.0, 0.0
        fee = self.fees.order_fee(pair.legs[venue], ex.vwap, ex.qty)
        self.cash[venue] -= ex.notional + fee
        pos = self.positions.setdefault(self._pkey(pair.pair_id, venue, side), Position(pair.pair_id, venue, side, opened_ts=ts))
        pos.qty += ex.qty
        pos.cost += ex.notional
        pos.fees += fee
        self.blotter.append("fills", asdict(Fill(ts, intent.intent_id, pair.pair_id, venue, side, "buy", ex.qty, ex.vwap, fee,
                                                 book.ts_recv, "intent")))
        return ex.qty, ex.notional, fee

    def _required_edge(self, intent: Intent) -> float:
        return max(0.0, intent.edge_seen * self.cfg.edge_retention)

    def _legs_in_order(self, intent: Intent) -> tuple[tuple[str, str, float], tuple[str, str, float]]:
        """(venue, side, seen level) for the leg sent first, then the other one."""
        a = (intent.venue_a, intent.side_a, intent.limit_a)
        b = (intent.venue_b, intent.side_b, intent.limit_b)
        first = intent.first_venue or self.cfg.first_leg
        return (a, b) if a[0] == first else (b, a)

    def _books_needed(self, intent: Intent) -> list[str]:
        if intent.model != "sequenced":
            return [intent.venue_a, intent.venue_b]
        (v1, _, _), (v2, _, _) = self._legs_in_order(intent)
        return [v1] if intent.stage == 1 else [v2]

    def _missed(self, intent: Intent, ts: float, poll: int, reason: str, cooldown: bool) -> None:
        intent.status = "missed"
        self.stats["missed"] += 1
        if cooldown:
            self.cooldown[intent.pair_id] = poll + self.cfg.failure_cooldown_polls
        self.blotter.append("intent_outcomes", {"ts": ts, "intent_id": intent.intent_id, "pair_id": intent.pair_id,
                                                "family": intent.family, "klass": intent.klass, "combo": intent.combo,
                                                "model": intent.model, "status": "missed", "reason": reason,
                                                "qty_intended": intent.qty, "qty_a": 0, "qty_b": 0, "hedged": 0,
                                                "edge_seen": intent.edge_seen, "polls_latency": poll - intent.created_poll})
        del self.intents[intent.intent_id]

    def _fill(self, intent: Intent, ts: float, books: dict[str, Book], pair: Pair | None, poll: int) -> None:
        if pair is None:
            self._missed(intent, ts, poll, "pair_gone", cooldown=False)
            return
        if any(books.get(v) is None for v in self._books_needed(intent)):
            # A venue did not answer this poll: our data gap, not a market miss. Wait a little. A
            # sequenced intent whose first leg is already on gets one more poll, then the missing
            # book is a leg failure, because the position exists whether the venue answers or not.
            grace = NO_BOOK_MAX_POLLS + (1 if intent.stage == 2 else 0)
            if poll - intent.created_poll < grace:
                return
            if intent.model == "sequenced" and intent.stage == 2:
                self._outcome_sequenced(intent, ts, poll, pair, 0, 0.0, 0.0, reason="no_book")
            else:
                self._missed(intent, ts, poll, "no_book", cooldown=False)
            return
        if intent.model == "sequenced":
            self._fill_sequenced(intent, ts, books, pair, poll)
        else:
            self._fill_joint(intent, ts, books, pair, poll)

    def _fill_joint(self, intent: Intent, ts: float, books: dict[str, Book], pair: Pair, poll: int) -> None:
        """Both legs against the same snapshot, sized by the same walk the scanner used, with the
        required edge in place of the scanner's minimum. What is still jointly profitable fills;
        what is not is left alone. A leg cannot be left naked, and a fill cannot cost more than
        the pair pays. This is the atomic upper bound on what a taker one poll late can capture."""
        va, sa, vb, sb = intent.venue_a, intent.side_a, intent.venue_b, intent.side_b
        ba, bb = books[va], books[vb]
        w = walk_pair(ba.asks(sa), bb.asks(sb), self._fee_fn(pair, va), self._fee_fn(pair, vb),
                      self._required_edge(intent), max_notional=1e12, min_level_size=0.0, max_qty=int(intent.qty))
        if w is None or w.qty <= 0:
            self._missed(intent, ts, poll, "edge_gone", cooldown=True)
            return
        qa, na, fa = self._buy(intent, pair, va, sa, ba, w.limit_a, ts, qty=w.qty)
        qb, nb, fb = self._buy(intent, pair, vb, sb, bb, w.limit_b, ts, qty=w.qty)
        self._outcome(intent, ts, poll, qa, qb, na, nb, fa, fb, reason=None)

    def _fill_sequenced(self, intent: Intent, ts: float, books: dict[str, Book], pair: Pair, poll: int) -> None:
        """Stage 1 (poll n+1): the first leg alone, allowed to spend the part of the detected edge
        that the retention setting does not reserve. Stage 2 (poll n+2): the other leg, with the
        limit that leaves the hedge its required edge after what the first leg actually paid.
        Whatever the second leg does not cover is unwound by the usual machinery."""
        (v1, s1, seen_1), (v2, s2, seen_2) = self._legs_in_order(intent)
        if intent.stage == 1:
            slack = intent.edge_seen * (1.0 - self.cfg.edge_retention)
            q1, n1, f1 = self._buy(intent, pair, v1, s1, books[v1], min(0.99, seen_1 + slack), ts, qty=int(intent.qty))
            if q1 <= 0:
                self._missed(intent, ts, poll, "first_leg_gone", cooldown=True)
                return
            intent.stage, intent.first_venue = 2, v1
            intent.qty_first, intent.cost_first, intent.fees_first = q1, n1, f1
            return
        paid_1 = (intent.cost_first + intent.fees_first) / intent.qty_first
        fee2 = self._fee_fn(pair, v2)
        limit_2 = 1.0 - paid_1 - self._required_edge(intent) - fee2(seen_2)
        limit_2 = min(0.99, 1.0 - paid_1 - self._required_edge(intent) - fee2(max(0.01, limit_2)))
        if limit_2 <= 0:
            q2, n2, f2 = 0, 0.0, 0.0
        else:
            q2, n2, f2 = self._buy(intent, pair, v2, s2, books[v2], limit_2, ts, qty=intent.qty_first)
        self._outcome_sequenced(intent, ts, poll, pair, q2, n2, f2, reason=None)

    def _outcome_sequenced(self, intent: Intent, ts: float, poll: int, pair: Pair, q2: int, n2: float, f2: float,
                           reason: str | None) -> None:
        q1, n1, f1 = intent.qty_first, intent.cost_first, intent.fees_first
        if intent.first_venue == intent.venue_a:
            self._outcome(intent, ts, poll, q1, q2, n1, n2, f1, f2, reason)
        else:
            self._outcome(intent, ts, poll, q2, q1, n2, n1, f2, f1, reason)

    def _outcome(self, intent: Intent, ts: float, poll: int, qa: int, qb: int, na: float, nb: float, fa: float, fb: float,
                 reason: str | None) -> None:
        hedged = min(qa, qb)
        excess_venue, excess_side, excess = ((intent.venue_a, intent.side_a, qa - qb) if qa > qb
                                             else (intent.venue_b, intent.side_b, qb - qa))
        # Cost of the hedge itself, per contract: each leg's average price and fee. The naked excess,
        # which the unwind sells back, is priced in the unwinds stream and must not be counted here.
        hedge_cost = ((na + fa) / qa + (nb + fb) / qb) if qa and qb else None
        record = {"ts": ts, "intent_id": intent.intent_id, "pair_id": intent.pair_id, "family": intent.family, "klass": intent.klass,
                  "combo": intent.combo, "model": intent.model, "qty_intended": intent.qty, "qty_a": qa, "qty_b": qb, "hedged": hedged,
                  "fill_ratio": round(hedged / intent.qty, 4) if intent.qty else None, "leg_failure": excess > 0,
                  "cost_a": round(na, 4), "cost_b": round(nb, 4), "fees": round(fa + fb, 4),
                  "hedge_cost_per_contract": round(hedge_cost, 5) if hedge_cost is not None else None,
                  # legacy: total cash out over hedged contracts, naked excess included. Kept for
                  # continuity of old rows; the report reads hedge_cost_per_contract first.
                  "locked_cost_per_hedged": round((na + nb + fa + fb) / hedged, 5) if hedged else None,
                  "edge_seen": intent.edge_seen,
                  "edge_kept": round(1.0 - hedge_cost, 5) if hedge_cost is not None else None,
                  "polls_latency": poll - intent.created_poll, "reason": reason}
        if qa == 0 and qb == 0:
            intent.status = "missed"
            self.stats["missed"] += 1
            self.cooldown[intent.pair_id] = poll + self.cfg.failure_cooldown_polls
        elif excess > 0:
            intent.status = "unwinding" if self.cfg.on_leg_failure == "unwind" else "partial"
            self.stats["partial"] += 1
            self.cooldown[intent.pair_id] = poll + self.cfg.failure_cooldown_polls
            if intent.status == "unwinding":
                self.unwind[intent.intent_id] = {"venue": excess_venue, "side": excess_side, "qty": excess, "polls": 0}
        else:
            intent.status = "filled"
            self.stats["filled"] += 1
        record["status"] = intent.status
        self.blotter.append("intent_outcomes", record)
        if intent.status in ("filled", "missed", "partial"):
            del self.intents[intent.intent_id]

    def _unwind(self, intent: Intent, ts: float, books: dict[str, Book], pair: Pair | None, poll: int) -> None:
        u = self.unwind.get(intent.intent_id)
        if not u or pair is None:
            self.intents.pop(intent.intent_id, None)
            return
        u["polls"] += 1
        pos_now = self.positions.get(self._pkey(pair.pair_id, u["venue"], u["side"]))
        if pos_now is None or pos_now.resolved or pos_now.voided or pos_now.qty <= 0:
            # Nothing left to sell: the venue paid the leg out (or it was voided) between the fill
            # and the unwind. The settlement has already booked whatever it was worth.
            self.intents.pop(intent.intent_id, None)
            self.unwind.pop(intent.intent_id, None)
            return
        book = books.get(u["venue"])
        if book is not None:
            ex = walk_sell(book.bids(u["side"]), self._fee_fn(pair, u["venue"]), int(u["qty"]))
            if ex.qty > 0:
                fee = self.fees.order_fee(pair.legs[u["venue"]], ex.vwap, ex.qty)
                pos = self.positions.get(self._pkey(pair.pair_id, u["venue"], u["side"]))
                if pos and pos.qty > 0:
                    avg = pos.avg_price
                    # The fee paid to buy these contracts leaves the position with them. Before 0.3.2
                    # it stayed on a position of zero quantity that could never settle: cash had
                    # gone, realized had not moved, and the leg failure looked cheaper than it was.
                    buy_fee = pos.fees * ex.qty / pos.qty
                    pos.qty -= ex.qty
                    pos.cost -= avg * ex.qty
                    pos.fees -= buy_fee
                    pnl = ex.notional - fee - buy_fee - avg * ex.qty
                    self.realized += pnl
                    self.cash[u["venue"]] += ex.notional - fee
                    self.blotter.append("fills", asdict(Fill(ts, intent.intent_id, pair.pair_id, u["venue"], u["side"], "sell",
                                                             ex.qty, ex.vwap, fee, book.ts_recv, "unwind")))
                    self.blotter.append("unwinds", {"ts": ts, "intent_id": intent.intent_id, "pair_id": pair.pair_id, "venue": u["venue"],
                                                    "side": u["side"], "qty": ex.qty, "vwap": round(ex.vwap, 4), "fee": round(fee, 4),
                                                    "buy_fee": round(buy_fee, 4), "pnl": round(pnl, 4), "polls": u["polls"]})
                u["qty"] -= ex.qty
        if u["qty"] <= 0:
            self.stats["unwound"] += 1
            self.intents.pop(intent.intent_id, None)
            self.unwind.pop(intent.intent_id, None)
        elif u["polls"] >= UNWIND_MAX_POLLS:
            self.stats["stuck"] += 1
            self.blotter.append("unwinds", {"ts": ts, "intent_id": intent.intent_id, "pair_id": pair.pair_id, "venue": u["venue"],
                                            "side": u["side"], "qty_left": u["qty"], "status": "stuck_hold_to_resolution"})
            self.intents.pop(intent.intent_id, None)
            self.unwind.pop(intent.intent_id, None)

    # ---- settlement ------------------------------------------------------------------------
    def open_venues(self, pair_id: str) -> set[str]:
        return {k.split("|")[1] for k, p in self.positions.items()
                if k.split("|")[0] == pair_id and not p.resolved and p.qty > 0}

    def _held_venues(self, pair_id: str) -> list[str]:
        return sorted({k.split("|")[1] for k, p in self.positions.items()
                       if k.split("|")[0] == pair_id and (p.qty > 0 or p.resolved) and not p.voided})

    def release_escrow(self, pair_id: str, ts: float, reason: str) -> dict[str, Any] | None:
        """Book an escrow balance whose pair has no open position and no definition any more. The
        balance is the whole result: every leg we held has been paid by its venue. Family is read
        from the pair id; class and key are unknown and left null."""
        esc = self.escrow.get(pair_id)
        if esc is None or self.open_venues(pair_id):
            return None
        self.realized += esc["pnl"]
        self.escrow.pop(pair_id, None)
        held = self._held_venues(pair_id)
        rec = {"ts": ts, "pair_id": pair_id, "family": pair_id.split("-")[0], "klass": None, "key": None,
               "yes_value": dict(self.settled.get(pair_id, {})), "divergent": None, "complete": True,
               "pending_venues": [], "venues_held": held, "naked": len(held) == 1, "definition_lost": True,
               "reason": reason, "pnl": round(esc["pnl"], 4), "pnl_legs": 0.0, "escrow": 0.0, "legs": []}
        self.blotter.append("resolutions", rec)
        return rec

    def settle(self, pair: Pair, yes_value: dict[str, float | None], ts: float) -> dict[str, Any] | None:
        """yes_value: venue -> settlement value of canonical YES (1, 0, or fractional), None if unresolved.

        Every verdict is recorded first, whether or not we hold anything on that venue, so a value
        that arrives while a leg is naked is not lost. Positions are then settled on every venue
        whose verdict is known, including verdicts recorded on an earlier call. The pair is complete
        when no venue on which we still hold an open position is without a verdict: a naked leg is
        whole on its own, its result is known the moment its venue pays, and waiting for the other
        venue would present a loss that is already certain as one that is not yet knowable."""
        known = self.settled.setdefault(pair.pair_id, {})
        known.update({v: float(x) for v, x in yes_value.items() if x is not None})
        touched = False
        legs_out: list[dict[str, Any]] = []
        pair_pnl = 0.0
        for venue, v in known.items():
            for side in ("yes", "no"):
                pos = self.positions.get(self._pkey(pair.pair_id, venue, side))
                if not pos or pos.resolved or pos.qty <= 0:
                    continue
                payout = pos.qty * (v if side == "yes" else 1.0 - v)
                pnl = payout - pos.cost - pos.fees
                pos.payout, pos.resolved = payout, True
                self.cash[venue] += payout          # the venue really paid: cash moves now
                pair_pnl += pnl
                touched = True
                legs_out.append({"venue": venue, "side": side, "qty": pos.qty, "cost": round(pos.cost, 4),
                                 "fees": round(pos.fees, 4), "payout": round(payout, 4), "pnl": round(pnl, 4),
                                 "yes_value": v, "days_locked": round((ts - pos.opened_ts) / 86400, 3)})
        values = list(known.values())
        pending = sorted(v for v in self.open_venues(pair.pair_id) if v not in known)
        complete = not pending
        if not touched and not (complete and pair.pair_id in self.escrow):
            return None                       # nothing settled and nothing to release: no event
        held = self._held_venues(pair.pair_id)
        # With a single venue reported, divergence is unknown, not absent: NULL, never False.
        divergent = (abs(values[0] - values[1]) > 1e-9) if len(values) == 2 else None
        # The unit of result is the pair, not the leg. Venues settle hours apart (Kalshi finalises
        # in minutes, Polymarket's oracle takes longer), so booking a leg on its own shows the
        # losing half of a hedge without its counterpart. Cash moves at settlement; the result
        # waits in escrow until every leg we hold has reported.
        esc = self.escrow.setdefault(pair.pair_id, {"pnl": 0.0, "since": ts})
        esc["pnl"] += pair_pnl
        booked: float | None = None
        if complete:
            booked = esc["pnl"]
            self.realized += booked
            self.escrow.pop(pair.pair_id, None)
        rec = {"ts": ts, "pair_id": pair.pair_id, "family": pair.family, "klass": pair.klass, "key": pair.key,
               "yes_value": dict(known), "divergent": divergent, "complete": complete, "pending_venues": pending,
               "venues_held": held, "naked": len(held) == 1,   # a naked leg is a leg failure's residue, not a hedge
               "pnl": round(booked, 4) if booked is not None else None,   # set only once the pair is whole
               "pnl_legs": round(pair_pnl, 4),                            # what this event alone moved
               "escrow": 0.0 if complete else round(esc["pnl"], 4),
               "legs": legs_out}
        self.blotter.append("resolutions", rec)
        return rec

    def observe(self, pair: Pair, yes_value: dict[str, float | None], ts: float) -> dict[str, Any] | None:
        """Record how a pair resolved without holding it. This is how classes we choose not to
        trade are still measured: the divergence rate, and the counterfactual return of a hedge
        priced from the detection log, with no capital deployed and no leg risk taken."""
        known = self.settled.setdefault(pair.pair_id, {})
        before = dict(known)
        known.update({v: float(x) for v, x in yes_value.items() if x is not None})
        if known == before:
            return None
        pending = sorted(v for v in pair.legs if v not in known)
        values = list(known.values())
        divergent = (abs(values[0] - values[1]) > 1e-9) if len(values) == 2 else None
        rec = {"ts": ts, "pair_id": pair.pair_id, "family": pair.family, "klass": pair.klass, "key": pair.key,
               "yes_value": dict(known), "divergent": divergent, "complete": not pending, "pending_venues": pending}
        self.blotter.append("observations", rec)
        return rec

    def write_off_zero_quantity_fees(self, ts: float) -> int:
        """Positions sold back in full by versions before 0.3.2 kept their buy fees on a zero
        quantity that no settlement would ever reach. Book those fees to realized once, so that
        cash = capital + realized + escrow - locked holds again, and close the positions."""
        n = 0
        for key, pos in self.positions.items():
            if pos.qty <= 0 and not pos.resolved and not pos.voided and pos.fees > 1e-9:
                self.realized -= pos.fees
                self.blotter.append("fee_writeoffs", {"ts": ts, "pair_id": pos.pair_id, "venue": pos.venue, "side": pos.side,
                                                      "fees": round(pos.fees, 4),
                                                      "reason": "buy fee of a leg sold back in full before 0.3.2"})
                pos.fees, pos.resolved = 0.0, True
                n += 1
        return n

    def force_complete_escrow(self, max_days: float, ts: float) -> list[dict[str, Any]]:
        """A pair half-settled for too long is booked as it stands, flagged. Without this, a venue
        that never reports (delisted, disputed) would hold a result in escrow for ever."""
        out = []
        for pid, esc in list(self.escrow.items()):
            if ts - esc["since"] < max_days * 86400:
                continue
            self.realized += esc["pnl"]
            rec = {"ts": ts, "pair_id": pid, "pnl": round(esc["pnl"], 4), "complete": False, "forced": True,
                   "days_waited": round((ts - esc["since"]) / 86400, 2),
                   "reason": "one venue never reported; booked as it stands"}
            self.blotter.append("escrow_forced", rec)
            self.escrow.pop(pid, None)
            out.append(rec)
        return out

    def void_pair(self, pair_id: str, reason: str, ts: float) -> dict[str, Any] | None:
        """Close every leg of a pair without claiming an outcome. Any settlement already booked on
        the pair is reversed first, the notional is returned, and only the fees actually paid stay
        in realized PnL. Used when a pair's definition is lost: one leg of a hedge settling while
        its counterpart can never settle books a loss that does not exist, so the honest treatment
        is to void the whole pair and exclude it from the study."""
        legs: list[dict[str, Any]] = []
        for k, pos in self.positions.items():
            if k.split("|")[0] != pair_id or pos.voided or pos.qty <= 0:
                continue
            was_settled = pos.resolved
            if was_settled:                                   # undo the settlement already recorded
                self.cash[pos.venue] -= pos.payout
                booked = pos.payout - pos.cost - pos.fees
                if pair_id in self.escrow:                    # still in escrow: never reached realized
                    self.escrow[pair_id]["pnl"] -= booked
                else:
                    self.realized -= booked
            self.cash[pos.venue] += pos.cost                  # give the notional back
            self.realized -= pos.fees                         # the fees were really paid
            legs.append({"venue": pos.venue, "side": pos.side, "qty": pos.qty, "cost": round(pos.cost, 4),
                         "fees": round(pos.fees, 4), "was_settled": was_settled})
            pos.resolved = pos.voided = True
            pos.payout = pos.cost
        if not legs:
            return None
        self.settled.pop(pair_id, None)
        self.escrow.pop(pair_id, None)
        rec = {"ts": ts, "pair_id": pair_id, "reason": reason, "legs": legs,
               "notional_returned": round(sum(l["cost"] for l in legs), 4),
               "fees_kept": round(sum(l["fees"] for l in legs), 4)}
        self.blotter.append("voids", rec)
        self.stats["voided"] = self.stats.get("voided", 0) + 1
        return rec

    def mark(self, books: dict[str, dict[str, Book]], ts: float) -> dict[str, Any]:
        unreal = 0.0        # open legs marked at the book mid
        congruent = 0.0     # open legs whose pair is half settled, marked at the outcome the other venue gave
        locked = 0.0
        n = 0
        for k, pos in self.positions.items():
            if pos.resolved or pos.qty <= 0:
                continue
            pair_id, venue, side = k.split("|")
            locked += pos.cost + pos.fees
            n += 1
            known = self.settled.get(pair_id, {})
            other = [v for vv, v in known.items() if vv != venue]
            if other:
                # The other venue has reported. A book mid is useless here (the market is closed);
                # what this leg will pay, if the two venues agree, is the number that matters. It is
                # a projection, not a result, and is reported apart from realized PnL.
                value = pos.qty * (other[0] if side == "yes" else 1.0 - other[0])
                congruent += value - pos.cost - pos.fees
                continue
            book = books.get(pair_id, {}).get(venue)
            mid = book.mid() if book else None
            if mid is None:
                continue
            value = pos.qty * (mid if side == "yes" else 1.0 - mid)
            unreal += value - pos.cost - pos.fees
        held = round(sum(e["pnl"] for e in self.escrow.values()), 2)
        rec = {"ts": ts, "open_positions": n, "locked": round(locked, 2), "unrealized": round(unreal, 2),
               "realized": round(self.realized, 2), "escrow": held, "escrow_pairs": len(self.escrow),
               "pending_if_congruent": round(congruent, 2),
               "total": round(self.realized + held + unreal, 2),
               "total_if_congruent": round(self.realized + held + unreal + congruent, 2),
               "cash": {v: round(c, 2) for v, c in self.cash.items()}}
        self.blotter.append("mtm", rec)
        return rec

    # ---- persistence -----------------------------------------------------------------------
    def state(self) -> dict[str, Any]:
        return {"cash": self.cash, "realized": self.realized, "stats": self.stats, "cooldown": self.cooldown,
                "settled": self.settled, "escrow": self.escrow,
                "positions": {k: asdict(p) for k, p in self.positions.items()},
                "intents": {k: asdict(i) for k, i in self.intents.items()}, "unwind": self.unwind}

    def load_state(self, s: dict[str, Any]) -> None:
        self.cash = {KALSHI: float(s["cash"].get(KALSHI, self.cash[KALSHI])),
                     POLYMARKET: float(s["cash"].get(POLYMARKET, self.cash[POLYMARKET]))}
        self.realized = float(s.get("realized", 0.0))
        self.stats.update(s.get("stats", {}))
        self.cooldown = {k: int(v) for k, v in s.get("cooldown", {}).items()}
        self.settled = {k: {vv: float(x) for vv, x in d.items()} for k, d in s.get("settled", {}).items()}
        self.escrow = {k: {"pnl": float(d["pnl"]), "since": float(d["since"])} for k, d in s.get("escrow", {}).items()}
        self.positions = {k: Position(**p) for k, p in s.get("positions", {}).items()}
        self.intents = {k: Intent(**i) for k, i in s.get("intents", {}).items()}
        self.unwind = dict(s.get("unwind", {}))
