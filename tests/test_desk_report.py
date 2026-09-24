"""End-to-end offline: fake venue clients feed the Desk for a few polls, then the report runs
on the files the desk produced."""
from __future__ import annotations

import gzip
import json
import time

import pytest

from conftest import kalshi_leg, poly_leg
from pmxarb.desk import Desk
from pmxarb.models import KALSHI, POLYMARKET, Pair
from pmxarb.report import Report


class FakeKalshi:
    def __init__(self):
        self.yes_bid, self.no_bid = 0.37, 0.57      # yes ask = 0.43

    async def get_orderbooks(self, tickers):
        return {t: {"ticker": t, "orderbook_fp": {"yes_dollars": [[f"{self.yes_bid:.2f}", "100.00"]],
                                                   "no_dollars": [[f"{self.no_bid:.2f}", "100.00"]]}} for t in tickers}

    async def get_market(self, ticker):
        return {"ticker": ticker, "status": "settled", "result": "yes"}

    async def aclose(self):
        pass


class FakePoly:
    def __init__(self):
        self.no_ask = 0.52

    async def get_books(self, token_ids):
        out = {}
        for t in token_ids:
            if t.endswith("yes"):
                out[t] = {"asset_id": t, "timestamp": str(int(time.time() * 1000)),
                          "bids": [{"price": "0.44", "size": "100"}], "asks": [{"price": "0.47", "size": "100"}]}
            else:
                out[t] = {"asset_id": t, "timestamp": str(int(time.time() * 1000)),
                          "bids": [{"price": "0.50", "size": "100"}], "asks": [{"price": f"{self.no_ask:.2f}", "size": "100"}]}
        return out

    async def clob_market(self, condition_id):
        return {"closed": True, "tokens": [{"token_id": "tok_yes", "winner": True, "price": 1}, {"token_id": "tok_no", "winner": False, "price": 0}]}

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_desk_polls_records_executes_and_reports(cfg, fees):
    cfg.recorder.gzip_after_minutes = 0
    k, p = FakeKalshi(), FakePoly()
    desk = Desk(cfg, k, p, fees)
    pair = Pair("sports-e2e", "sports", "sports|NFL|2026-09-18|DAL", "exact",
                {KALSHI: kalshi_leg(close_ts=time.time() - 10), POLYMARKET: poly_leg(cat="sports", close_ts=time.time() - 10)},
                created_ts=time.time())
    desk.set_pairs([pair])
    # poll 1: 0.43 + 0.52 + fees ~ 0.975 -> opportunity, intent created
    await desk.poll()
    assert desk.paper.stats["intents"] == 1
    # poll 2: same books -> filled
    await desk.poll()
    assert desk.paper.stats["filled"] == 1
    # poll 3: books unchanged -> no new snapshot line (changes mode), exposure blocks nothing but cap
    await desk.poll()
    snaps = list((cfg.data / "snapshots").glob("*/*.jsonl"))
    assert len(snaps) == 1
    lines = snaps[0].read_text().splitlines()
    assert len(lines) == 1                                 # unchanged books are not rewritten
    rec = json.loads(lines[0])
    assert rec["k"]["ya"][0][0] == pytest.approx(0.43) and rec["p"]["na"][0][0] == pytest.approx(0.52)
    # blotter streams exist
    for s in ("polls", "detections", "intents", "intent_outcomes", "fills"):
        assert (cfg.data / "blotter" / f"{s}.jsonl").exists(), s
    # settlement sweep: pair closed in the past, both venues say DAL won
    n = await desk.sweep_resolutions()
    assert n == 1
    res = [json.loads(l) for l in (cfg.data / "blotter" / "resolutions.jsonl").read_text().splitlines()]
    assert res[0]["divergent"] is False and res[0]["pnl"] > 0
    assert desk.paper.open_pairs() == set()
    # state survives a restart
    desk.close()
    desk2 = Desk(cfg, k, p, fees)
    assert desk2.poll_idx == 3 and desk2.paper.realized == pytest.approx(desk.paper.realized)
    # compression of the closed snapshot file
    desk2.snapshots.close()
    assert desk2.compress() == 1
    gz = list((cfg.data / "snapshots").glob("*/*.jsonl.gz"))
    assert gz and gzip.open(gz[0]).read()
    # report for today
    desk2.mark_to_market()
    from datetime import datetime, timezone
    text, digest = Report(cfg, datetime.now(timezone.utc)).build()
    assert "## Detections" in text and "## Paper execution" in text
    assert "filled" in text and "sports" in text
    assert "detections" in digest
    path, _ = Report(cfg, datetime.now(timezone.utc)).write()
    assert path.exists() and (cfg.data / "reports" / "latest.md").exists()


@pytest.mark.asyncio
async def test_held_pair_survives_restart_after_leaving_the_universe(cfg, fees):
    """Regression: a pair holding positions must keep its definition across a restart even when the
    daily universe no longer lists it, otherwise its legs can never be settled and the capital is
    stuck with a permanently one-sided PnL."""
    from pmxarb.matching.matcher import save_pairs
    cfg.paper.execute_classes = ["exact", "basis"]

    k, p = FakeKalshi(), FakePoly()
    desk = Desk(cfg, k, p, fees)
    pair = Pair("crypto-expiring", "crypto", "crypto|BTC|above|78000|2026-09-18T16:00Z", "basis",
                {KALSHI: kalshi_leg(close_ts=time.time() - 10), POLYMARKET: poly_leg(cat="crypto", close_ts=time.time() - 10)},
                created_ts=time.time())
    save_pairs(cfg, [pair], {}, {})                 # daily archive, used by the recovery path
    desk.set_pairs([pair])
    await desk.poll()
    await desk.poll()                               # both legs filled
    assert desk.paper.open_pairs() == {pair.pair_id}
    desk.close()

    # next day: the pair expired and is absent from the new universe; the desk restarts empty
    other = Pair("sports-new", "sports", "sports|NFL|2026-09-20|PHI", "exact",
                 {KALSHI: kalshi_leg(ticker="K2"), POLYMARKET: poly_leg(cond="0xdef", cat="sports", yes="y2", no="n2")},
                 created_ts=time.time())
    desk2 = Desk(cfg, k, p, fees)
    assert pair.pair_id in desk2.all_pairs(), "held pair lost on restart"
    desk2.set_pairs([other])
    assert pair.pair_id in desk2.all_pairs(), "held pair dropped by the universe refresh"
    assert desk2.carried() == {pair.pair_id}
    n = await desk2.sweep_resolutions()
    assert n == 1                                    # it can settle, which was impossible before
    assert desk2.paper.open_pairs() == set()
    # one hedge per pair means no second intent was ever stacked while the first was held
    assert not desk2.paper.intents and desk2.carried() == set()
    desk2.set_pairs([other])
    assert pair.pair_id not in desk2.all_pairs()     # nothing held any more: forgotten


@pytest.mark.asyncio
async def test_pair_definition_recovered_from_universe_archive(cfg, fees):
    """A state written before the fix has no carried pairs; the definition is rebuilt from the
    daily universe archive so today's orphaned positions can still settle."""
    import json as _json
    from pmxarb.matching.matcher import save_pairs
    cfg.paper.execute_classes = ["exact", "basis"]

    k, p = FakeKalshi(), FakePoly()
    desk = Desk(cfg, k, p, fees)
    pair = Pair("crypto-orphan", "crypto", "crypto|BTC|above|82000|2026-09-18T16:00Z", "basis",
                {KALSHI: kalshi_leg(close_ts=time.time() - 10), POLYMARKET: poly_leg(cat="crypto", close_ts=time.time() - 10)},
                created_ts=time.time())
    save_pairs(cfg, [pair], {}, {})
    desk.set_pairs([pair])
    await desk.poll()
    await desk.poll()
    desk.close()
    # simulate the old on-disk format: positions kept, pair definitions dropped
    sp = cfg.path("state", "paper_state.json")
    s = _json.loads(sp.read_text())
    s.pop("carried", None)
    s["retired"] = []
    sp.write_text(_json.dumps(s))

    desk2 = Desk(cfg, k, p, fees)
    assert pair.pair_id in desk2.all_pairs()
    assert await desk2.sweep_resolutions() == 1


class SplitPoly(FakePoly):
    """Polymarket that reports its settlement only when told to, to exercise escrow."""

    def __init__(self):
        super().__init__()
        self.settled = False

    async def clob_market(self, condition_id):
        if not self.settled:
            return {"closed": False, "tokens": []}
        return await super().clob_market(condition_id)


@pytest.mark.asyncio
async def test_full_cycle_exact_traded_basis_observed_and_counterfactual(cfg, fees):
    """The design in one run: the exact pair is traded and its result booked only once both venues
    report; the basis pair is detected and its outcome recorded, but never held; the report prices
    the counterfactual of the basis hedge from the detection log."""
    from datetime import datetime, timezone

    k, p = FakeKalshi(), SplitPoly()
    desk = Desk(cfg, k, p, fees)
    past = time.time() - 10
    exact = Pair("sports-x", "sports", "sports|NFL|2026-09-20|PHI", "exact",
                 {KALSHI: kalshi_leg(close_ts=past), POLYMARKET: poly_leg(cat="sports", close_ts=past)}, created_ts=past)
    basis = Pair("crypto-b", "crypto", "crypto|BTC|above|78000|2026-09-20T16:00Z", "basis",
                 {KALSHI: kalshi_leg(ticker="K2", close_ts=past),
                  POLYMARKET: poly_leg(cond="0xdef", cat="crypto", close_ts=past)}, created_ts=past)
    desk.set_pairs([exact, basis])
    await desk.poll()
    await desk.poll()

    # only the exact pair got capital; the basis pair was seen but not held
    assert desk.paper.open_pairs() == {"sports-x"}
    assert "crypto-b" in desk.detected and "sports-x" in desk.detected

    # Kalshi settles first: the result waits in escrow, realized stays untouched
    n = await desk.sweep_resolutions()
    assert n == 2                                            # one settled, one observed
    assert desk.paper.realized == 0.0 and desk.paper.escrow.get("sports-x")
    res = [json.loads(l) for l in (cfg.data / "blotter" / "resolutions.jsonl").read_text().splitlines()]
    assert res[-1]["pnl"] is None and res[-1]["complete"] is False
    obs = [json.loads(l) for l in (cfg.data / "blotter" / "observations.jsonl").read_text().splitlines()]
    assert obs[-1]["pair_id"] == "crypto-b" and obs[-1]["complete"] is False

    # Polymarket reports: both pairs complete, the result is booked whole
    p.settled = True
    await desk.sweep_resolutions()
    assert desk.paper.escrow == {} and desk.paper.realized > 0
    assert desk.paper.open_pairs() == set()
    assert desk.detected == set()                            # both outcomes recorded, nothing left to watch
    obs = [json.loads(l) for l in (cfg.data / "blotter" / "observations.jsonl").read_text().splitlines()]
    assert obs[-1]["complete"] is True and obs[-1]["divergent"] is False

    # the report prices the basis hedge we never took
    desk.mark_to_market()
    text, _ = Report(cfg, datetime.now(timezone.utc)).build()
    assert "## Classes not traded: counterfactual" in text
    assert "divergence_rate" in text and "mean_pnl_per_contract" in text
    assert "basis" in text.split("## Classes not traded")[1]
