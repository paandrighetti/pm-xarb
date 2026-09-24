from __future__ import annotations

import math

import pytest

from conftest import kalshi_leg, poly_leg
from pmxarb.books import kalshi_book, polymarket_book, walk_buy, walk_pair, walk_sell
from pmxarb.models import Level


def test_kalshi_fee_matches_schedule(fees):
    leg = kalshi_leg()
    assert fees.order_fee(leg, 0.50, 100) == pytest.approx(1.75)
    # 7 cents x 0.20 x 0.80 x 100 = 1.12 exactly
    assert fees.order_fee(leg, 0.20, 100) == pytest.approx(1.12)
    # rounding is upward to a centicent
    raw = 0.07 * 0.33 * 0.67 * 7
    assert fees.order_fee(leg, 0.33, 7) >= raw
    assert fees.order_fee(leg, 0.33, 7) - raw < 0.0001 + 1e-9
    # series multiplier scales linearly
    assert fees.order_fee(kalshi_leg(mult=0.5), 0.50, 100) == pytest.approx(0.875)


def test_polymarket_fee_by_category(fees):
    assert fees.order_fee(poly_leg(cat="crypto"), 0.50, 100) == pytest.approx(1.75)
    assert fees.order_fee(poly_leg(cat="sports"), 0.50, 100) == pytest.approx(1.25)
    assert fees.order_fee(poly_leg(cat="politics"), 0.50, 100) == pytest.approx(1.00)
    assert fees.order_fee(poly_leg(cat="geopolitics"), 0.50, 100) == 0.0
    assert fees.order_fee(poly_leg(cat="unknown-tag"), 0.50, 100) == pytest.approx(1.25)  # conservative fallback
    assert fees.poly_category(["nfl", "sports"]) == "sports"
    assert fees.poly_category(["something"]) == "other"


def test_kalshi_book_mirrors_bids_into_asks():
    payload = {"ticker": "T", "orderbook_fp": {"yes_dollars": [["0.40", "100.00"], ["0.42", "50.00"]],
                                                "no_dollars": [["0.55", "30.00"], ["0.50", "80.00"]]}}
    b = kalshi_book(kalshi_leg(), payload, ts_recv=1.0)
    assert b.yes_bids[0] == Level(0.42, 50.0)             # best bid first
    assert b.no_bids[0] == Level(0.55, 30.0)
    assert b.yes_asks[0] == Level(0.45, 30.0)             # 1 - best NO bid, same size
    assert b.yes_asks[1] == Level(0.50, 80.0)
    assert b.no_asks[0] == Level(0.58, 50.0)              # 1 - best YES bid
    assert b.mid() == pytest.approx((0.42 + 0.45) / 2)


def test_kalshi_book_polarity_flip():
    payload = {"ticker": "T", "orderbook_fp": {"yes_dollars": [["0.40", "100.00"]], "no_dollars": [["0.55", "30.00"]]}}
    b = kalshi_book(kalshi_leg(yes_is_no=True), payload, ts_recv=1.0)
    assert b.yes_bids[0] == Level(0.55, 30.0)             # canonical YES = venue NO
    assert b.yes_asks[0] == Level(0.60, 100.0)


def test_kalshi_legacy_cents_payload():
    payload = {"ticker": "T", "orderbook": {"yes": [[40, 100], [42, 50]], "no": [[55, 30]]}}
    b = kalshi_book(kalshi_leg(), payload, ts_recv=1.0)
    assert b.yes_bids[0] == Level(0.42, 50.0)
    assert b.yes_asks[0] == Level(0.45, 30.0)


def test_polymarket_book_sorted_best_first():
    yes = {"asset_id": "tok_yes", "timestamp": "1700000000123",
           "bids": [{"price": "0.38", "size": "10"}, {"price": "0.40", "size": "20"}],
           "asks": [{"price": "0.45", "size": "5"}, {"price": "0.43", "size": "15"}]}
    no = {"asset_id": "tok_no", "bids": [{"price": "0.55", "size": "7"}], "asks": [{"price": "0.58", "size": "9"}]}
    b = polymarket_book(poly_leg(), yes, no, ts_recv=2.0)
    assert b.yes_bids[0] == Level(0.40, 20.0)
    assert b.yes_asks[0] == Level(0.43, 15.0)
    assert b.no_asks[0] == Level(0.58, 9.0)
    assert b.ts_src == pytest.approx(1700000000.123)
    # without the NO book, complement of the YES ladder
    b2 = polymarket_book(poly_leg(), yes, None, ts_recv=2.0)
    assert b2.no_asks[0] == Level(0.60, 20.0)


def test_walk_pair_takes_only_profitable_depth(fees):
    lk, lp = kalshi_leg(), poly_leg(cat="sports")
    fk = lambda p: fees.marginal_fee(lk, p)  # noqa: E731
    fp = lambda p: fees.marginal_fee(lp, p)  # noqa: E731
    asks_k = [Level(0.40, 100), Level(0.44, 100)]
    asks_p = [Level(0.55, 50), Level(0.58, 100)]
    w = walk_pair(asks_k, asks_p, fk, fp, min_edge=0.002, max_notional=500)
    assert w is not None
    assert w.qty == 50                                    # second poly level kills the edge
    assert w.limit_a == 0.40 and w.limit_b == 0.55
    expected_edge = 1 - (0.40 + fk(0.40) + 0.55 + fp(0.55))
    assert w.marginal_edge == pytest.approx(expected_edge)
    assert w.edge_total == pytest.approx(50 * expected_edge)
    # notional cap binds
    w2 = walk_pair(asks_k, asks_p, fk, fp, min_edge=0.002, max_notional=20)
    assert w2.qty == math.floor(20 / (0.40 + fk(0.40) + 0.55 + fp(0.55)))
    # no edge -> None
    assert walk_pair([Level(0.50, 100)], [Level(0.52, 100)], fk, fp, 0.002, 500) is None
    # stale tiny level is skipped
    w3 = walk_pair([Level(0.10, 1), Level(0.40, 100)], asks_p, fk, fp, 0.002, 500, min_level_size=2)
    assert w3.limit_a == 0.40


def test_walk_buy_and_sell_respect_limits():
    fee = lambda p: 0.0  # noqa: E731
    asks = [Level(0.40, 10), Level(0.42, 10), Level(0.50, 100)]
    ex = walk_buy(asks, fee, 25, limit=0.42)
    assert ex.qty == 20 and ex.vwap == pytest.approx(0.41)
    assert walk_buy(asks, fee, 25, limit=0.39).qty == 0
    bids = [Level(0.39, 5), Level(0.35, 100)]
    s = walk_sell(bids, fee, 8)
    assert s.qty == 8 and s.notional == pytest.approx(5 * 0.39 + 3 * 0.35)
