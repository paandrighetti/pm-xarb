from __future__ import annotations

import pytest

from pmxarb.venues.base import HttpError
from pmxarb.venues.kalshi import KalshiClient, dollars, fixed
from pmxarb.venues.polymarket import PolymarketClient, jlist


def test_kalshi_field_readers():
    assert dollars({"yes_bid_dollars": "0.4200", "yes_bid": 42}, "yes_bid") == 0.42
    assert dollars({"yes_bid": 42}, "yes_bid") == 0.42
    assert dollars({}, "yes_bid") is None
    assert fixed({"volume_fp": "123.00"}, "volume") == 123.0
    assert fixed({"volume": 7}, "volume") == 7.0
    assert jlist('["Yes", "No"]') == ["Yes", "No"] and jlist(None) == [] and jlist(["a"]) == ["a"]


@pytest.mark.asyncio
async def test_kalshi_orderbooks_falls_back_to_repeated_params(cfg):
    k = KalshiClient(cfg.venues.kalshi)
    calls = []

    async def fake_request(method, path, params=None, **kw):
        calls.append(params)
        if isinstance(params, dict):                      # comma form: server answers only the first ticker
            first = params["tickers"].split(",")[0]
            return {"orderbooks": [{"ticker": first, "orderbook_fp": {"yes_dollars": [], "no_dollars": []}}]}
        return {"orderbooks": [{"ticker": t, "orderbook_fp": {"yes_dollars": [], "no_dollars": []}} for _, t in params]}

    k.request = fake_request  # type: ignore[assignment]
    out = await k.get_orderbooks(["A", "B", "C"])
    assert set(out) == {"A", "B", "C"}
    assert k._tickers_repeated is True
    out2 = await k.get_orderbooks(["A", "B"])
    assert set(out2) == {"A", "B"} and isinstance(calls[-1], list)
    await k.aclose()


@pytest.mark.asyncio
async def test_kalshi_orderbooks_comma_form_kept_when_it_works(cfg):
    k = KalshiClient(cfg.venues.kalshi)

    async def fake_request(method, path, params=None, **kw):
        assert isinstance(params, dict)
        return {"orderbooks": [{"ticker": t, "orderbook_fp": {}} for t in params["tickers"].split(",")]}

    k.request = fake_request  # type: ignore[assignment]
    assert set(await k.get_orderbooks(["A", "B"])) == {"A", "B"}
    assert k._tickers_repeated is False
    await k.aclose()


@pytest.mark.asyncio
async def test_polymarket_events_keyset_pagination(cfg):
    p = PolymarketClient(cfg.venues.polymarket)
    pages = {None: ({"events": [{"id": i, "active": True} for i in range(3)] + [{"id": 99, "active": False}], "next_cursor": "c1"}),
             "c1": ({"events": [{"id": i} for i in range(3, 5)]})}          # last page: no next_cursor
    seen = []

    async def fake_request(method, path, params=None, **kw):
        assert path == "/events/keyset" and "offset" not in params and params["limit"] <= 500
        seen.append(params.get("after_cursor"))
        return pages[params.get("after_cursor")]

    p.request = fake_request  # type: ignore[assignment]
    evs = await p.list_events()
    assert [e["id"] for e in evs] == [0, 1, 2, 3, 4]      # inactive event filtered client-side
    assert seen == [None, "c1"]
    await p.aclose()


@pytest.mark.asyncio
async def test_polymarket_events_keyset_drops_order_on_422(cfg):
    p = PolymarketClient(cfg.venues.polymarket)
    calls = []

    async def fake_request(method, path, params=None, **kw):
        calls.append(dict(params))
        if "order" in params:
            raise HttpError(422, "u", "validation error")
        return {"events": [{"id": 1}]}

    p.request = fake_request  # type: ignore[assignment]
    assert len(await p.list_events()) == 1
    assert "order" in calls[0] and "order" not in calls[1]
    await p.aclose()


@pytest.mark.asyncio
async def test_http_error_is_raised_not_swallowed(cfg):
    k = KalshiClient(cfg.venues.kalshi)

    async def fake_request(method, path, params=None, **kw):
        raise HttpError(403, "u", "blocked")

    k.request = fake_request  # type: ignore[assignment]
    with pytest.raises(HttpError):
        await k.get_orderbooks(["A"])
    await k.aclose()
