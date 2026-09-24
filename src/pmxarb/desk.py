"""One poll = fetch both venues concurrently, normalize, record, execute pending intents against
the fresh books, scan, create intents, persist state. Everything downstream reads the files."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from .books import kalshi_book, polymarket_book
from .config import Config
from .fees import FeeModel
from .models import KALSHI, POLYMARKET, Book, Pair
from .paper import PaperDesk
from .recorder import Blotter, JsonlWriter, atomic_json
from .resolution import yes_values
from .scanner import Scanner
from .venues import HttpError, KalshiClient, PolymarketClient

log = logging.getLogger(__name__)


class Desk:
    def __init__(self, cfg: Config, kalshi: KalshiClient, poly: PolymarketClient, fees: FeeModel):
        self.cfg = cfg
        self.kalshi = kalshi
        self.poly = poly
        self.fees = fees
        self.blotter = Blotter(cfg.data / "blotter")
        self.snapshots = JsonlWriter(cfg.data / "snapshots", cfg.recorder.rotate_minutes)
        self.scanner = Scanner(cfg.scanner, fees, cfg.paper.max_notional_per_pair_usd)
        self.paper = PaperDesk(cfg.paper, fees, self.blotter)
        self.active: dict[str, Pair] = {}
        self.retired: dict[str, Pair] = {}          # pairs we still carry but that left the universe
        self.detected: set[str] = set()             # pairs seen with an opportunity, outcome not yet recorded
        self.poll_idx = 0
        self.last_books: dict[str, dict[str, Book]] = {}
        self._fingerprints: dict[str, tuple] = {}
        self.health: dict[str, Any] = {"polls": 0, "errors": 0, "last_ok": None, "skew_s_sum": 0.0, "missing_books": 0}
        self._state_path = cfg.path("state", "paper_state.json")
        self._violations_seen: set[str] = set()
        self.pending_alerts: list[str] = []
        self._load()
        self.pending_alerts.extend(self.check_invariants())

    # ---- pairs -----------------------------------------------------------------------------
    def carried(self) -> set[str]:
        """Pairs whose definition must survive whatever the universe does: we hold something in
        them, their result sits in escrow, or we watched an opportunity on them and their outcome
        is not recorded yet. Losing any of these means a leg that can never settle, a result that
        can never be booked, or a measurement that can never be completed. Escrow was missing from
        this set until 0.3: a naked leg settled and parked there had no open position left, so the
        next universe rebuild dropped its definition and the sweep never looked at it again."""
        return (self.paper.open_pairs() | {i.pair_id for i in self.paper.intents.values()}
                | set(self.paper.escrow) | set(self.detected))

    def set_pairs(self, pairs: list[Pair]) -> None:
        new = {p.pair_id: p for p in pairs}
        keep = self.carried()
        for pid, p in list(self.active.items()) + list(self.retired.items()):
            if pid not in new and pid in keep:
                self.retired[pid] = p
        self.active = new
        self.retired = {pid: p for pid, p in self.retired.items() if pid in keep and pid not in new}
        orphans = keep - set(self.active) - set(self.retired)
        if orphans:
            log.warning("positions with no pair definition (cannot settle): %s", sorted(orphans))
        log.info("desk: %d active pairs, %d carried out of universe", len(self.active), len(self.retired))

    def all_pairs(self) -> dict[str, Pair]:
        d = dict(self.retired)
        d.update(self.active)
        return d

    # ---- one poll --------------------------------------------------------------------------
    async def fetch_books(self, pairs: dict[str, Pair]) -> dict[str, dict[str, Book]]:
        k_tickers = sorted({p.legs[KALSHI].market_id for p in pairs.values()})
        p_tokens = sorted({t for p in pairs.values() for t in (p.legs[POLYMARKET].yes_token, p.legs[POLYMARKET].no_token) if t})
        t_k0 = time.time()

        async def timed(coro):
            res = await coro                 # completion time measured inside the task, not at the await
            return res, time.time()

        k_task = asyncio.create_task(timed(self.kalshi.get_orderbooks(k_tickers))) if k_tickers else None
        p_task = asyncio.create_task(timed(self.poly.get_books(p_tokens))) if p_tokens else None
        k_raw: dict[str, dict] = {}
        p_raw: dict[str, dict] = {}
        k_ts = p_ts = t_k0
        if k_task:
            try:
                k_raw, k_ts = await k_task
            except HttpError as exc:
                log.warning("kalshi books failed: %s", exc)
                self.health["errors"] += 1
        if p_task:
            try:
                p_raw, p_ts = await p_task
            except HttpError as exc:
                log.warning("polymarket books failed: %s", exc)
                self.health["errors"] += 1
        self.health["skew_s_sum"] += abs(k_ts - p_ts)
        out: dict[str, dict[str, Book]] = {}
        for pid, pair in pairs.items():
            books: dict[str, Book] = {}
            lk, lp = pair.legs[KALSHI], pair.legs[POLYMARKET]
            if lk.market_id in k_raw:
                books[KALSHI] = kalshi_book(lk, k_raw[lk.market_id], k_ts)
            else:
                self.health["missing_books"] += 1
            yb, nb = p_raw.get(lp.yes_token or ""), p_raw.get(lp.no_token or "")
            if yb or nb:
                books[POLYMARKET] = polymarket_book(lp, yb, nb, p_ts)
            else:
                self.health["missing_books"] += 1
            out[pid] = books
        return out

    def record(self, ts: float, books: dict[str, dict[str, Book]]) -> None:
        depth = self.cfg.recorder.depth_levels
        for pid, bv in books.items():
            if not bv:
                continue
            fp = tuple(sorted((v, b.fingerprint(depth)) for v, b in bv.items()))
            if self.cfg.recorder.record_mode == "changes" and self._fingerprints.get(pid) == fp:
                continue
            self._fingerprints[pid] = fp
            self.snapshots.write({"ts": round(ts, 3), "poll": self.poll_idx, "pair": pid,
                                  "k": bv[KALSHI].compact(depth) if KALSHI in bv else None,
                                  "p": bv[POLYMARKET].compact(depth) if POLYMARKET in bv else None}, ts)

    async def poll(self) -> None:
        self.poll_idx += 1
        ts = time.time()
        pairs = self.all_pairs()
        if not pairs:
            return
        books = await self.fetch_books(pairs)
        self.last_books = books
        self.record(ts, books)
        # 1. pending intents meet the fresh books
        self.paper.on_snapshot(self.poll_idx, ts, books, pairs)
        # 2. scan active pairs only
        opps = []
        for pid, pair in self.active.items():
            opps.extend(self.scanner.scan_pair(pair, books.get(pid, {}), ts))
        for o in opps:
            self.blotter.append("detections", o.to_dict())
            self.detected.add(o.pair_id)
        for ep in self.scanner.track(opps, ts):
            self.blotter.append("episodes", ep)
        # 3. new intents
        cutoff = max([b.ts_recv for bv in books.values() for b in bv.values()] or [ts])
        self.paper.consider(self.poll_idx, ts, opps, pairs, cutoff)
        self.health["polls"] += 1
        self.health["last_ok"] = ts
        n_k = sum(1 for bv in books.values() if KALSHI in bv)
        n_p = sum(1 for bv in books.values() if POLYMARKET in bv)
        k_ts = [bv[KALSHI].ts_recv for bv in books.values() if KALSHI in bv]
        p_ts = [bv[POLYMARKET].ts_recv for bv in books.values() if POLYMARKET in bv]
        self.blotter.append("polls", {"ts": round(ts, 3), "poll": self.poll_idx, "pairs": len(pairs), "active": len(self.active),
                                      "books_k": n_k, "books_p": n_p, "detections": len(opps),
                                      "skew_s": round(abs(k_ts[0] - p_ts[0]), 3) if k_ts and p_ts else None,
                                      "latency_s": round(time.time() - ts, 3)})
        self.pending_alerts.extend(self.check_invariants(pairs))
        self._save()

    def drain_alerts(self) -> list[str]:
        out, self.pending_alerts = self.pending_alerts, []
        return out

    def check_invariants(self, pairs: dict[str, Pair] | None = None) -> list[str]:
        """Run the paper desk's invariants; open the circuit breaker on a violation. Returns the
        violations that are new since the last check, for the runner to alert on."""
        violations = self.paper.invariants(pairs if pairs is not None else self.all_pairs())
        new = [v for v in violations if v not in self._violations_seen]
        if violations:
            if self.paper.halted is None:
                log.error("INVARIANT VIOLATION, execution halted (recording continues): %s", "; ".join(violations))
            self.paper.halted = "; ".join(violations)
            self.blotter.append("invariants", {"ts": time.time(), "poll": self.poll_idx, "violations": violations})
        elif self.paper.halted is not None:
            log.warning("invariants hold again; execution resumes")
            self.paper.halted = None
        self._violations_seen = set(violations)
        return new

    # ---- periodic jobs ---------------------------------------------------------------------
    async def sweep_resolutions(self) -> int:
        now = time.time()
        n = 0
        pairs = self.all_pairs()
        # What we hold, what sits in escrow (a pair whose legs are all settled or gone but whose
        # result is not booked yet), and what we merely watched: a class we chose not to trade is
        # still measured, from its outcome and the detection log.
        held = self.paper.open_pairs() | set(self.paper.escrow)
        todo = [pid for pid in sorted(set(self.detected) | held)
                if pid in pairs
                and min(l.close_ts for l in pairs[pid].legs.values()) <= now
                and (pid in self.paper.escrow or len(self.paper.settled.get(pid, {})) < len(pairs[pid].legs))]
        # Escrow entries whose definition is gone and on which nothing is open any more: the result
        # is already known (it is the escrow balance), and no venue can be asked anything useful.
        # Book them as they stand rather than wait seven days for the forced release.
        for pid in sorted(set(self.paper.escrow) - set(pairs)):
            if not self.paper.open_venues(pid):
                rec = self.paper.release_escrow(pid, now, reason="pair definition lost; nothing open; booked as it stands")
                if rec:
                    n += 1
                    log.warning("escrow released without definition on %s: %s", pid, rec["pnl"])
        for pid in todo:
            pair = pairs[pid]
            values = await yes_values(self.kalshi, self.poly, pair.legs)
            if pid in held:
                rec = self.paper.settle(pair, values, now)
                if rec:
                    n += 1
                    log.info("settled %s %s pnl=%s escrow=%s divergent=%s", pid, values, rec["pnl"],
                             rec["escrow"], rec["divergent"])
            else:
                rec = self.paper.observe(pair, values, now)
                if rec:
                    n += 1
                    log.info("observed %s %s divergent=%s complete=%s", pid, values, rec["divergent"], rec["complete"])
            if rec and rec["complete"]:
                self.detected.discard(pid)
        for r in self.paper.force_complete_escrow(self.cfg.resolution.escrow_max_days, now):
            log.warning("escrow forced on %s after %s days: %s", r["pair_id"], r["days_waited"], r["pnl"])
        self.set_pairs(list(self.active.values()))
        self._save()
        return n

    def orphans(self) -> set[str]:
        """Pairs we hold something in whose definition is unknown: they can never be settled."""
        return self.carried() - set(self.all_pairs())

    def void_orphans(self, reason: str) -> list[dict[str, Any]]:
        out = []
        for pid in sorted(self.orphans()):
            rec = self.paper.void_pair(pid, reason, time.time())
            if rec:
                out.append(rec)
                log.warning("voided %s: %s returned, %s fees kept", pid, rec["notional_returned"], rec["fees_kept"])
        for iid, intent in list(self.paper.intents.items()):
            if intent.pair_id not in self.all_pairs():
                del self.paper.intents[iid]
                self.paper.unwind.pop(iid, None)
        self.detected -= (self.detected - set(self.all_pairs()))
        self._save()
        return out

    def mark_to_market(self) -> dict[str, Any]:
        return self.paper.mark(self.last_books, time.time())

    def compress(self) -> int:
        return self.snapshots.compress_closed(self.cfg.recorder.gzip_after_minutes * 60)

    # ---- state -----------------------------------------------------------------------------
    def _save(self) -> None:
        """Persist the definition of every pair we carry, active or not. `self.active` is rebuilt
        from pairs.json at startup, so a pair that is both held and gone from the universe would
        otherwise be lost across a restart and its legs could never settle."""
        known = self.all_pairs()
        carried = [known[pid].to_dict() for pid in sorted(self.carried()) if pid in known]
        atomic_json(self._state_path, {"poll_idx": self.poll_idx, "paper": self.paper.state(),
                                        "carried": carried, "detected": sorted(self.detected),
                                        "health": self.health, "saved_ts": time.time()})

    def _recover_pairs(self, missing: set[str]) -> dict[str, Pair]:
        """Rebuild Pair definitions from the daily universe archives, newest first."""
        found: dict[str, Pair] = {}
        for f in sorted((self.cfg.data / "universe").glob("pairs_*.json"), reverse=True):
            if not missing - found.keys():
                break
            try:
                payload = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for d in payload.get("pairs", []):
                if d.get("pair_id") in missing and d["pair_id"] not in found:
                    found[d["pair_id"]] = Pair.from_dict(d)
        return found

    def _load(self) -> None:
        if not self._state_path.exists():
            return
        try:
            s = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("state unreadable, starting fresh: %s", exc)
            return
        self.poll_idx = int(s.get("poll_idx", 0))
        self.paper.load_state(s.get("paper", {}))
        self.retired = {d["pair_id"]: Pair.from_dict(d) for d in s.get("carried", s.get("retired", []))}
        self.detected = set(s.get("detected", []))
        self.health.update(s.get("health", {}))
        missing = self.carried() - set(self.retired)
        if missing:
            rec = self._recover_pairs(missing)
            self.retired.update(rec)
            if rec:
                log.info("recovered %d pair definitions from the universe archives: %s", len(rec), sorted(rec))
            lost = missing - rec.keys()
            if lost:
                log.error("positions with no recoverable pair definition: %s", sorted(lost))
        written_off = self.paper.write_off_zero_quantity_fees(time.time())
        if written_off:
            log.warning("wrote off the buy fees of %d legs sold back in full by an earlier version; realized now %.2f",
                        written_off, self.paper.realized)
            self._save()          # the blotter rows are written; the state must say so before anything else runs
        log.info("state restored: poll %d, cash %s, %d positions, %d carried pairs",
                 self.poll_idx, self.paper.cash, len(self.paper.positions), len(self.retired))

    def close(self) -> None:
        for ep in self.scanner.flush():
            self.blotter.append("episodes", ep)
        self.snapshots.close()
        self._save()
