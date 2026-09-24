from __future__ import annotations

import json

import pytest

from conftest import kalshi_leg, poly_leg
from pmxarb.models import KALSHI, POLYMARKET, Book, Level, Pair
from pmxarb.paper import PaperDesk
from pmxarb.recorder import Blotter
from pmxarb.scanner import Scanner


def make_pair(klass="exact") -> Pair:
    return Pair("sports-test", "sports", "sports|NFL|2026-09-18|DAL", klass,
                {KALSHI: kalshi_leg(), POLYMARKET: poly_leg(cat="sports")}, created_ts=1.0)


def books(k_yes_ask, k_yes_size, p_no_ask, p_no_size, ts=10.0, p_no_bid=None, k_yes_bid=None):
    kb = Book(KALSHI, "k", ts, None,
              yes_bids=[Level(k_yes_bid or k_yes_ask - 0.03, 100)], yes_asks=[Level(k_yes_ask, k_yes_size)],
              no_bids=[Level(1 - k_yes_ask, k_yes_size)], no_asks=[Level(1 - (k_yes_bid or k_yes_ask - 0.03), 100)])
    pb = Book(POLYMARKET, "p", ts, ts,
              yes_bids=[Level(1 - p_no_ask - 0.02, 100)], yes_asks=[Level(1 - (p_no_bid or p_no_ask - 0.03), 100)],
              no_bids=[Level(p_no_bid or p_no_ask - 0.03, 100)], no_asks=[Level(p_no_ask, p_no_size)])
    return {"sports-test": {KALSHI: kb, POLYMARKET: pb}}


def read(blotter_dir, stream):
    p = blotter_dir / f"{stream}.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def test_hedged_fill_then_exact_settlement(cfg, fees, tmp_path):
    bl = Blotter(tmp_path / "blotter")
    desk = PaperDesk(cfg.paper, fees, bl)
    sc = Scanner(cfg.scanner, fees, cfg.paper.max_notional_per_pair_usd)
    pair = make_pair()
    pairs = {pair.pair_id: pair}
    b1 = books(0.40, 100, 0.55, 100, ts=10.0)
    opps = sc.scan_pair(pair, b1["sports-test"], 10.0)
    assert len(opps) == 1 and opps[0].combo == "kalshi:yes+polymarket:no"
    created = desk.consider(1, 10.0, opps, pairs, 10.0)
    assert len(created) == 1
    intent = created[0]
    assert intent.qty == 100                              # depth 100, notional cap 500 not binding
    cash0 = dict(desk.cash)
    desk.on_snapshot(1, 10.0, b1, pairs)                  # same poll: nothing happens
    assert desk.cash == cash0
    desk.on_snapshot(2, 13.0, b1, pairs)                  # next poll: both legs fill
    outcomes = read(bl.root, "intent_outcomes")
    assert outcomes[-1]["status"] == "filled" and outcomes[-1]["hedged"] == 100
    assert desk.cash[KALSHI] < cash0[KALSHI] and desk.cash[POLYMARKET] < cash0[POLYMARKET]
    locked = sum(p.cost + p.fees for p in desk.positions.values())
    assert desk.exposure(pair.pair_id) == pytest.approx(locked)
    # no second intent while capital is locked in the pair beyond the cap? cap is 500, exposure ~97: allowed again
    # settle: DAL wins on both venues -> kalshi YES pays 1, poly NO pays 0 -> total 100
    rec = desk.settle(pair, {KALSHI: 1.0, POLYMARKET: 1.0}, 20.0)
    assert rec is not None and rec["divergent"] is False
    expected = 100.0 - locked
    assert rec["pnl"] == pytest.approx(expected, abs=1e-6)
    assert desk.realized == pytest.approx(expected, abs=1e-6)
    assert desk.cash[KALSHI] + desk.cash[POLYMARKET] == pytest.approx(2 * cfg.paper.capital_per_venue_usd + expected, abs=1e-6)
    assert desk.open_pairs() == set()


def test_leg_failure_is_unwound_at_market(cfg, fees, tmp_path):
    bl = Blotter(tmp_path / "blotter")
    desk = PaperDesk(cfg.paper, fees, bl)
    sc = Scanner(cfg.scanner, fees, cfg.paper.max_notional_per_pair_usd)
    pair = make_pair()
    pairs = {pair.pair_id: pair}
    b1 = books(0.40, 100, 0.55, 100, ts=10.0)
    desk.consider(1, 10.0, sc.scan_pair(pair, b1["sports-test"], 10.0), pairs, 10.0)
    # poly ask jumped beyond the limit before our order arrived; kalshi still there
    b2 = books(0.40, 100, 0.70, 100, ts=13.0)
    desk.on_snapshot(2, 13.0, b2, pairs)
    out = read(bl.root, "intent_outcomes")[-1]
    assert out["status"] == "unwinding" and out["qty_a"] == 100 and out["qty_b"] == 0
    assert desk.cooldown[pair.pair_id] >= 2
    # next poll: naked kalshi YES sold into the bid (0.37) -> loss of spread plus fees
    b3 = books(0.40, 100, 0.70, 100, ts=16.0)
    desk.on_snapshot(3, 16.0, b3, pairs)
    unw = read(bl.root, "unwinds")[-1]
    assert unw["qty"] == 100 and unw["pnl"] < 0
    assert desk.positions[f"{pair.pair_id}|kalshi|yes"].qty == 0
    assert desk.stats["unwound"] == 1 and not desk.intents
    # cooldown blocks a new intent on the same pair
    assert desk.consider(4, 19.0, sc.scan_pair(pair, b1["sports-test"], 19.0), pairs, 19.0) == []


def test_basis_pair_can_diverge(cfg, fees, tmp_path):
    cfg.paper.execute_classes = ["exact", "basis"]   # this test exercises settlement, not policy
    bl = Blotter(tmp_path / "blotter")
    desk = PaperDesk(cfg.paper, fees, bl)
    sc = Scanner(cfg.scanner, fees, cfg.paper.max_notional_per_pair_usd)
    pair = make_pair(klass="basis")
    pairs = {pair.pair_id: pair}
    b1 = books(0.40, 100, 0.55, 100, ts=10.0)
    desk.consider(1, 10.0, sc.scan_pair(pair, b1["sports-test"], 10.0), pairs, 10.0)
    desk.on_snapshot(2, 13.0, b1, pairs)
    locked = sum(p.cost + p.fees for p in desk.positions.values())
    # both legs lose: kalshi says NO (yes value 0), polymarket says YES (our NO loses)
    rec = desk.settle(pair, {KALSHI: 0.0, POLYMARKET: 1.0}, 20.0)
    assert rec["divergent"] is True
    assert rec["pnl"] == pytest.approx(-locked)


def test_capital_and_pair_caps_bound_size(cfg, fees, tmp_path):
    cfg.paper.max_notional_per_pair_usd = 50
    bl = Blotter(tmp_path / "blotter")
    desk = PaperDesk(cfg.paper, fees, bl)
    sc = Scanner(cfg.scanner, fees, cfg.paper.max_notional_per_pair_usd)
    pair = make_pair()
    pairs = {pair.pair_id: pair}
    b1 = books(0.40, 1000, 0.55, 1000, ts=10.0)
    opps = sc.scan_pair(pair, b1["sports-test"], 10.0)
    created = desk.consider(1, 10.0, opps, pairs, 10.0)
    assert created and created[0].qty * (created[0].limit_a + created[0].limit_b) <= 50 + 1e-9
    # exhaust cash on one venue
    desk.cash[POLYMARKET] = 5.0
    desk.intents.clear()
    created = desk.consider(2, 13.0, opps, pairs, 13.0)
    assert created and created[0].qty <= 5.0 // (0.56 * 1.02)


def test_state_roundtrip(cfg, fees, tmp_path):
    bl = Blotter(tmp_path / "blotter")
    desk = PaperDesk(cfg.paper, fees, bl)
    sc = Scanner(cfg.scanner, fees, cfg.paper.max_notional_per_pair_usd)
    pair = make_pair()
    pairs = {pair.pair_id: pair}
    b1 = books(0.40, 100, 0.55, 100, ts=10.0)
    desk.consider(1, 10.0, sc.scan_pair(pair, b1["sports-test"], 10.0), pairs, 10.0)
    desk.on_snapshot(2, 13.0, b1, pairs)
    s = json.loads(json.dumps(desk.state()))
    d2 = PaperDesk(cfg.paper, fees, bl)
    d2.load_state(s)
    assert d2.cash == pytest.approx(desk.cash)
    assert set(d2.positions) == set(desk.positions)
    assert d2.exposure(pair.pair_id) == pytest.approx(desk.exposure(pair.pair_id))


def test_scanner_episodes(cfg, fees):
    sc = Scanner(cfg.scanner, fees, 500)
    pair = make_pair()
    b1 = books(0.40, 100, 0.55, 100, ts=10.0)
    o = sc.scan_pair(pair, b1["sports-test"], 10.0)
    assert sc.track(o, 10.0) == []
    assert sc.track(o, 13.0) == []
    assert sc.track([], 16.0) == []          # first miss
    ended = sc.track([], 19.0)               # second miss closes the episode
    assert len(ended) == 1 and ended[0]["polls"] == 2 and ended[0]["lifetime_s"] == pytest.approx(3.0)


def test_settlement_of_one_venue_does_not_claim_absence_of_divergence(cfg, fees, tmp_path):
    cfg.paper.execute_classes = ["exact", "basis"]   # this test exercises settlement, not policy
    bl = Blotter(tmp_path / "blotter")
    desk = PaperDesk(cfg.paper, fees, bl)
    sc = Scanner(cfg.scanner, fees, cfg.paper.max_notional_per_pair_usd)
    pair = make_pair(klass="basis")
    pairs = {pair.pair_id: pair}
    b1 = books(0.40, 100, 0.55, 100, ts=10.0)
    desk.consider(1, 10.0, sc.scan_pair(pair, b1["sports-test"], 10.0), pairs, 10.0)
    desk.on_snapshot(2, 13.0, b1, pairs)
    # only Kalshi has reported: its leg settles, the hedge stays open, divergence is unknown
    rec = desk.settle(pair, {KALSHI: 0.0, POLYMARKET: None}, 20.0)
    assert rec["divergent"] is None and rec["complete"] is False and rec["pending_venues"] == [POLYMARKET]
    assert rec["pnl"] is None                                 # no result yet: half a hedge is not a result
    assert rec["pnl_legs"] < 0 and rec["escrow"] < 0          # the losing leg is held in escrow
    assert desk.realized == 0.0                               # and never touches realized PnL
    assert desk.open_pairs() == {pair.pair_id}                # the paying leg is still held
    # Polymarket reports the same outcome later: the hedge pays and the pair ends up positive
    rec2 = desk.settle(pair, {POLYMARKET: 0.0}, 30.0)
    assert rec2["divergent"] is False and rec2["complete"] is True and rec2["pending_venues"] == []
    assert rec2["pnl"] > 0 and rec2["escrow"] == 0.0
    assert desk.realized == pytest.approx(rec2["pnl"])        # booked once, whole
    assert desk.escrow == {}
    # every dollar is accounted for: starting capital plus the pair's result, nothing locked
    assert sum(desk.cash.values()) == pytest.approx(2 * cfg.paper.capital_per_venue_usd + desk.realized)
    assert desk.open_pairs() == set()


def test_escrow_is_forced_when_a_venue_never_reports(cfg, fees, tmp_path):
    cfg.paper.execute_classes = ["exact", "basis"]   # this test exercises settlement, not policy
    bl = Blotter(tmp_path / "blotter")
    desk = PaperDesk(cfg.paper, fees, bl)
    sc = Scanner(cfg.scanner, fees, cfg.paper.max_notional_per_pair_usd)
    pair = make_pair(klass="basis")
    pairs = {pair.pair_id: pair}
    b1 = books(0.40, 100, 0.55, 100, ts=10.0)
    desk.consider(1, 10.0, sc.scan_pair(pair, b1["sports-test"], 10.0), pairs, 10.0)
    desk.on_snapshot(2, 13.0, b1, pairs)
    desk.settle(pair, {KALSHI: 0.0, POLYMARKET: None}, 20.0)
    held = desk.escrow[pair.pair_id]["pnl"]
    assert desk.force_complete_escrow(max_days=7, ts=20.0 + 3 * 86400) == []      # too early
    forced = desk.force_complete_escrow(max_days=7, ts=20.0 + 8 * 86400)
    assert len(forced) == 1 and forced[0]["forced"] is True and forced[0]["complete"] is False
    assert desk.realized == pytest.approx(held) and desk.escrow == {}


def test_class_cap_keeps_room_for_exact_pairs(cfg, fees, tmp_path):
    cfg.paper.execute_classes = ["exact", "basis"]
    cfg.paper.max_notional_per_class_usd = {"exact": 8000, "basis": 60}
    bl = Blotter(tmp_path / "blotter")
    desk = PaperDesk(cfg.paper, fees, bl)
    sc = Scanner(cfg.scanner, fees, cfg.paper.max_notional_per_pair_usd)
    basis = make_pair(klass="basis")
    exact = Pair("sports-exact", "sports", "sports|NFL|2026-09-20|PHI", "exact",
                 {KALSHI: kalshi_leg(ticker="K2"), POLYMARKET: poly_leg(cond="0xdef", cat="sports", yes="y2", no="n2")}, created_ts=1.0)
    pairs = {basis.pair_id: basis, exact.pair_id: exact}
    b = books(0.40, 1000, 0.55, 1000, ts=10.0)
    opps = sc.scan_pair(basis, b["sports-test"], 10.0) + [o for o in sc.scan_pair(exact, b["sports-test"], 10.0)]
    for o in opps:
        o.pair_id = basis.pair_id if o is opps[0] else exact.pair_id
    created = desk.consider(1, 10.0, opps, pairs, 10.0)
    by_pair = {i.pair_id: i for i in created}
    assert by_pair[basis.pair_id].qty * (by_pair[basis.pair_id].limit_a + by_pair[basis.pair_id].limit_b) <= 60 + 1e-9
    # the exact pair is only bound by the per-pair cap, not by the basis ceiling
    assert by_pair[exact.pair_id].qty * (by_pair[exact.pair_id].limit_a + by_pair[exact.pair_id].limit_b) > 60
    used = desk.class_exposure(pairs)
    assert used["basis"] <= 60 + 1e-9


def test_voiding_a_half_settled_pair_reverses_the_false_loss(cfg, fees, tmp_path):
    """A hedge whose counterpart can never settle books a loss that does not exist. Voiding must
    reverse it: notional back, only the fees actually paid left in realized PnL."""
    cfg.paper.execute_classes = ["exact", "basis"]   # this test exercises settlement, not policy
    bl = Blotter(tmp_path / "blotter")
    desk = PaperDesk(cfg.paper, fees, bl)
    sc = Scanner(cfg.scanner, fees, cfg.paper.max_notional_per_pair_usd)
    pair = make_pair(klass="basis")
    pairs = {pair.pair_id: pair}
    b1 = books(0.40, 100, 0.55, 100, ts=10.0)
    cash0 = dict(desk.cash)
    desk.consider(1, 10.0, sc.scan_pair(pair, b1["sports-test"], 10.0), pairs, 10.0)
    desk.on_snapshot(2, 13.0, b1, pairs)
    total_fees = sum(p.fees for p in desk.positions.values())
    # only the losing leg settles; the paying leg is orphaned
    desk.settle(pair, {KALSHI: 0.0, POLYMARKET: None}, 20.0)
    assert desk.realized == 0.0 and desk.escrow[pair.pair_id]["pnl"] < -40   # held, not booked
    rec = desk.void_pair(pair.pair_id, "definition lost", 30.0)
    assert rec is not None and len(rec["legs"]) == 2
    assert any(l["was_settled"] for l in rec["legs"]) and any(not l["was_settled"] for l in rec["legs"])
    # PnL now carries the fees and nothing else; capital is whole again
    assert desk.realized == pytest.approx(-total_fees, abs=1e-6)
    assert desk.cash[KALSHI] + desk.cash[POLYMARKET] == pytest.approx(cash0[KALSHI] + cash0[POLYMARKET] - total_fees, abs=1e-6)
    assert desk.open_pairs() == set() and desk.escrow == {}
    assert desk.void_pair(pair.pair_id, "again", 40.0) is None   # idempotent
    assert read(bl.root, "voids")[-1]["pair_id"] == pair.pair_id


def test_basis_pairs_are_observed_not_traded_by_default(cfg, fees, tmp_path):
    """Policy: only `exact` pairs get capital. A basis pair is a bet that two different
    measurements of the same event agree, not an arbitrage, so it is detected and recorded but
    never held. The detection still reaches the blotter, which is what the study needs."""
    assert cfg.paper.execute_classes == ["exact"]
    bl = Blotter(tmp_path / "blotter")
    desk = PaperDesk(cfg.paper, fees, bl)
    sc = Scanner(cfg.scanner, fees, cfg.paper.max_notional_per_pair_usd)
    b1 = books(0.40, 100, 0.55, 100, ts=10.0)
    basis = make_pair(klass="basis")
    opps = sc.scan_pair(basis, b1["sports-test"], 10.0)
    assert opps, "the opportunity is still detected"
    assert desk.consider(1, 10.0, opps, {basis.pair_id: basis}, 10.0) == []
    assert desk.cash == {KALSHI: cfg.paper.capital_per_venue_usd, POLYMARKET: cfg.paper.capital_per_venue_usd}
    exact = make_pair(klass="exact")
    assert desk.consider(2, 13.0, sc.scan_pair(exact, b1["sports-test"], 13.0), {exact.pair_id: exact}, 13.0)


def test_only_one_hedge_per_pair_at_a_time(cfg, fees, tmp_path):
    bl = Blotter(tmp_path / "blotter")
    desk = PaperDesk(cfg.paper, fees, bl)
    sc = Scanner(cfg.scanner, fees, cfg.paper.max_notional_per_pair_usd)
    pair = make_pair()
    pairs = {pair.pair_id: pair}
    b1 = books(0.40, 100, 0.55, 100, ts=10.0)
    desk.consider(1, 10.0, sc.scan_pair(pair, b1["sports-test"], 10.0), pairs, 10.0)
    desk.on_snapshot(2, 13.0, b1, pairs)
    assert desk.open_pairs() == {pair.pair_id}
    desk.cooldown.clear()
    # the opposite combo now looks attractive; the desk must not stack a second, opposing hedge
    b2 = books(0.55, 100, 0.40, 100, ts=16.0)
    assert desk.consider(3, 16.0, sc.scan_pair(pair, b2["sports-test"], 16.0), pairs, 16.0) == []
    desk.settle(pair, {KALSHI: 1.0, POLYMARKET: 1.0}, 20.0)
    assert desk.open_pairs() == set()
    assert desk.consider(4, 23.0, sc.scan_pair(pair, b1["sports-test"], 23.0), pairs, 23.0)
