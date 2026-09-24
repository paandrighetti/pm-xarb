"""Historical screen, labelled UPPER BOUND everywhere it is printed.

Kalshi exposes 1-minute candles with yes_bid and yes_ask closes; Polymarket exposes a price
series with no depth and no side. We combine Kalshi's real touch with a Polymarket touch
assumed at mid +/- poly_half_spread. Minutes where the combined cost, fees included, is below 1
are counted. Nothing here is executable evidence: the Polymarket side is an assumption and the
two series are aligned to the minute, not to the millisecond. The output is used to decide where
to point the recorder, not to publish a return.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from .config import Config
from .fees import FeeModel
from .models import KALSHI, POLYMARKET, Pair
from .venues import HttpError, KalshiClient, PolymarketClient

log = logging.getLogger(__name__)
FFILL_MAX_MIN = 10


def _candle_close(c: dict, key: str) -> float | None:
    sub = c.get(key) or {}
    v = sub.get("close_dollars")
    if v not in (None, ""):
        return float(v)
    v = sub.get("close")
    if v in (None, ""):
        return None
    v = float(v)
    return v / 100.0 if v > 1.0 else v


def _minute_series(points: list[tuple[int, float]], t0: int, t1: int) -> dict[int, float]:
    """Forward-fill onto a 1-minute grid, gaps capped at FFILL_MAX_MIN."""
    pts = sorted(points)
    out: dict[int, float] = {}
    i = 0
    last_t, last_v = None, None
    for t in range(t0 - t0 % 60, t1, 60):
        while i < len(pts) and pts[i][0] <= t:
            last_t, last_v = pts[i]
            i += 1
        if last_v is not None and last_t is not None and t - last_t <= FFILL_MAX_MIN * 60:
            out[t] = last_v
    return out


async def screen_pair(cfg: Config, kalshi: KalshiClient, poly: PolymarketClient, fees: FeeModel, pair: Pair,
                      t0: int, t1: int) -> dict[str, Any] | None:
    lk, lp = pair.legs[KALSHI], pair.legs[POLYMARKET]
    try:
        candles = await kalshi.get_candles(lk.fee_category, lk.market_id, t0, t1, period=1)
        hist = await poly.prices_history(lp.yes_token or "", t0, t1, fidelity_min=1)
    except HttpError as exc:
        log.warning("history %s: %s", pair.pair_id, exc)
        return None
    k_bid, k_ask = [], []
    for c in candles:
        t = int(c.get("end_period_ts") or 0)
        b, a = _candle_close(c, "yes_bid"), _candle_close(c, "yes_ask")
        if t and b is not None:
            k_bid.append((t, b))
        if t and a is not None:
            k_ask.append((t, a))
    p_mid = [(int(h["t"]), float(h["p"])) for h in hist if h.get("t") is not None and h.get("p") is not None]
    kb, ka, pm = _minute_series(k_bid, t0, t1), _minute_series(k_ask, t0, t1), _minute_series(p_mid, t0, t1)
    if lk.yes_is_venue_no:
        kb, ka = {t: 1 - v for t, v in ka.items()}, {t: 1 - v for t, v in kb.items()}
    h = cfg.history.poly_half_spread
    fk = lambda p: fees.marginal_fee(lk, p)  # noqa: E731
    fp = lambda p: fees.marginal_fee(lp, p)  # noqa: E731
    minutes = sorted(set(kb) & set(ka) & set(pm))
    if not minutes:
        return {"pair_id": pair.pair_id, "family": pair.family, "klass": pair.klass, "minutes": 0}
    pos1 = pos2 = 0
    sum1 = sum2 = 0.0
    max1 = max2 = 0.0
    run = best_run = 0
    for t in minutes:
        k_yes_ask, k_no_ask = ka[t], 1.0 - kb[t]
        p_yes_ask, p_no_ask = min(0.99, pm[t] + h), min(0.99, 1.0 - pm[t] + h)
        e1 = 1.0 - (k_yes_ask + fk(k_yes_ask) + p_no_ask + fp(p_no_ask))     # kalshi yes + poly no
        e2 = 1.0 - (k_no_ask + fk(k_no_ask) + p_yes_ask + fp(p_yes_ask))     # kalshi no + poly yes
        any_pos = False
        if e1 > 0:
            pos1 += 1; sum1 += e1; max1 = max(max1, e1); any_pos = True
        if e2 > 0:
            pos2 += 1; sum2 += e2; max2 = max(max2, e2); any_pos = True
        run = run + 1 if any_pos else 0
        best_run = max(best_run, run)
    return {"pair_id": pair.pair_id, "family": pair.family, "klass": pair.klass, "key": pair.key,
            "minutes": len(minutes), "coverage": round(len(minutes) / max(1, (t1 - t0) // 60), 3),
            "pos_minutes_k_yes": pos1, "mean_edge_k_yes": round(sum1 / pos1, 4) if pos1 else 0.0, "max_edge_k_yes": round(max1, 4),
            "pos_minutes_k_no": pos2, "mean_edge_k_no": round(sum2 / pos2, 4) if pos2 else 0.0, "max_edge_k_no": round(max2, 4),
            "longest_run_min": best_run, "share_positive": round(max(pos1, pos2) / len(minutes), 4)}


async def run_screen(cfg: Config, kalshi: KalshiClient, poly: PolymarketClient, fees: FeeModel, pairs: list[Pair]) -> tuple[str, list[dict]]:
    t1 = int(time.time())
    t0 = t1 - cfg.history.days * 86400
    ranked = sorted(pairs, key=lambda p: -(p.legs[KALSHI].volume + p.legs[POLYMARKET].volume))[: cfg.history.top_pairs]
    rows: list[dict] = []
    for p in ranked:
        r = await screen_pair(cfg, kalshi, poly, fees, p, max(t0, int(p.created_ts) - cfg.history.days * 86400), min(t1, int(p.close_ts) or t1))
        if r:
            rows.append(r)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if rows:
        pq.write_table(pa.Table.from_pylist(rows), cfg.path("history", f"screen_{day}.parquet"))
    md = [f"# Historical screen, {day} (UPPER BOUND, not executable)\n",
          f"Window: last {cfg.history.days} days, Kalshi 1-minute yes_bid/yes_ask closes vs Polymarket price series "
          f"assumed at mid +/- {cfg.history.poly_half_spread}. Fees included, depth ignored, alignment to the minute.\n",
          "| pair | family | klass | minutes | coverage | +min K.yes | mean | max | +min K.no | mean | max | longest run |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: -(r.get("pos_minutes_k_yes", 0) + r.get("pos_minutes_k_no", 0))):
        md.append(f"| {r['pair_id']} | {r['family']} | {r['klass']} | {r.get('minutes', 0)} | {r.get('coverage', 0)} | "
                  f"{r.get('pos_minutes_k_yes', 0)} | {r.get('mean_edge_k_yes', 0)} | {r.get('max_edge_k_yes', 0)} | "
                  f"{r.get('pos_minutes_k_no', 0)} | {r.get('mean_edge_k_no', 0)} | {r.get('max_edge_k_no', 0)} | {r.get('longest_run_min', 0)} |")
    text = "\n".join(md) + "\n"
    cfg.path("history", f"screen_{day}.md").write_text(text, encoding="utf-8")
    return text, rows
