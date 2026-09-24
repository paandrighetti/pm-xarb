"""Deterministic parsers. Each venue object is reduced to a CanonLeg: a canonical key that two
venues can only share if they describe the same event, plus the Leg needed to trade it.

Three families in v1:
  crypto  : "<asset> above/below <threshold> at <instant>"  (Kalshi strike fields, Polymarket regex)
  macro   : FOMC decision kind per meeting month; CPI month-over-month threshold per reference month
  sports  : "<team> wins <league> game on <date>"           (NFL, MLB, NBA moneylines)

A parser returns nothing rather than guessing. Coverage is reported by the universe job so that
gaps are visible instead of silently matched.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from ..config import UniverseCfg
from ..fees import FeeModel
from ..models import KALSHI, POLYMARKET, Leg
from ..venues.kalshi import dollars, fixed
from ..venues.polymarket import jlist
from . import normalize as N


@dataclass
class CanonLeg:
    family: str
    key: str                 # exact key
    block: str               # loose-join block
    when: datetime | None    # reference instant / game date for loose joins
    leg: Leg
    sources: set[str] = field(default_factory=set)
    meta: dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------------------------------------
# Kalshi
# ------------------------------------------------------------------------------------------------

def _k_close(m: dict) -> datetime | None:
    return N.parse_iso(m.get("close_time")) or N.parse_iso(m.get("expected_expiration_time"))


def _k_leg(m: dict, series: dict, title: str, yes_label: str, yes_is_no: bool = False) -> Leg:
    src = ", ".join(s.get("name", "") for s in (series.get("settlement_sources") or []) if isinstance(s, dict))
    close = _k_close(m)
    return Leg(
        venue=KALSHI, market_id=m["ticker"], title=title, yes_label=yes_label,
        fee_category=series.get("ticker", m.get("series_ticker", "")),
        fee_multiplier=float(series.get("fee_multiplier") or 1.0),
        close_ts=N.to_ts(close) or 0.0, resolution_source=src,
        resolution_ts=N.to_ts(N.parse_iso(m.get("expected_expiration_time"))),
        yes_is_venue_no=yes_is_no, volume=fixed(m, "volume"),
        extra={"event_ticker": m.get("event_ticker"), "fee_type": series.get("fee_type"),
               "rules": (m.get("rules_primary") or "")[:400]},
    )


def kalshi_crypto(m: dict, series: dict) -> CanonLeg | None:
    st = str(m.get("series_ticker") or series.get("ticker") or m.get("ticker", "")).upper()
    am = re.search(r"KX(BTC|ETH|SOL|XRP)", st)
    if not am:
        return None
    asset = am.group(1)
    strike_type = (m.get("strike_type") or "").lower()
    floor_, cap = m.get("floor_strike"), m.get("cap_strike")
    when = _k_close(m)
    if not when:
        return None
    if strike_type in ("greater", "greater_or_equal") and floor_ is not None:
        kind, thr = "above", float(floor_)
    elif strike_type in ("less", "less_or_equal") and cap is not None:
        kind, thr = "below", float(cap)
    elif strike_type == "between" and floor_ is not None and cap is not None:
        kind, thr = "between", (float(floor_), float(cap))
    else:
        return None
    thr_s = f"{thr[0]:g}-{thr[1]:g}" if isinstance(thr, tuple) else f"{thr:g}"
    instant = when.astimezone(N.UTC).strftime("%Y-%m-%dT%H:%MZ")
    key = f"crypto|{asset}|{kind}|{thr_s}|{instant}"
    block = f"crypto|{asset}|{kind}|{thr_s}|{N.et_date(when).isoformat()}"
    title = f"{m.get('title', '')} {m.get('yes_sub_title') or m.get('subtitle') or ''}".strip()
    leg = _k_leg(m, series, title, yes_label="Yes")
    src = N.source_tokens(leg.resolution_source) | N.source_tokens(m.get("rules_primary"))
    return CanonLeg("crypto", key, block, when, leg, src,
                    {"asset": asset, "kind": kind, "threshold": thr_s, "instant": instant})


_FOMC_CUT = re.compile(r"\b(cut\w*|lower\w*|decreas\w*|reduc\w*)\b")
_FOMC_HIKE = re.compile(r"\b(hik\w*|rais\w*|increas\w*)\b")
_FOMC_HOLD = re.compile(r"\b(no change|hold\w*|unchanged|maintain\w*|pause\w*|same)\b")
_BPS = re.compile(r"(>=|≥|>|more than|over|at least)?\s*(\d+)\s*(\+|or more|or higher|or greater|at least)?"
                  r"\s*(?:bps|bp|basis points?)\b")
FOMC_STEP = 25


def fomc_kind(text: str) -> str | None:
    """Canonical decision kind. The set of outcomes must be identical on both venues:
    'cut 25 bps' is cut_25; '50+ bps', 'at least 50' are cut_50plus; Kalshi's '>25bps' is
    strictly more than 25, i.e. the next 25 bp step or more, hence cut_50plus; '0bps' is hold."""
    t = text.lower()
    m = _BPS.search(t)
    bps = int(m.group(2)) if m else None
    if _FOMC_HOLD.search(t) or bps == 0:
        return "hold"
    if m is None:
        return None
    strict = m.group(1) in (">", "more than", "over")
    open_ended = bool(m.group(3)) or m.group(1) in (">=", "≥", "at least")
    if strict:
        bps, open_ended = bps + FOMC_STEP, True
    suffix = "plus" if open_ended else ""
    if _FOMC_CUT.search(t):
        return f"cut_{bps}{suffix}"
    if _FOMC_HIKE.search(t):
        return f"hike_{bps}{suffix}"
    return None


def kalshi_macro(m: dict, series: dict) -> CanonLeg | None:
    st = str(m.get("series_ticker") or series.get("ticker") or "").upper()
    when = _k_close(m)
    if not when:
        return None
    text = " ".join(str(m.get(k) or "") for k in ("title", "subtitle", "yes_sub_title"))
    title = text.strip()
    if "FED" in st:
        kind = fomc_kind(text)
        ym = N.ticker_month(m.get("event_ticker")) or (when.year, when.month)
        if not kind:
            return None
        period = f"{ym[0]:04d}-{ym[1]:02d}"
        key = f"macro|fomc|{period}|{kind}"
        leg = _k_leg(m, series, title, yes_label="Yes")
        return CanonLeg("macro", key, key, when, leg, {"fed"} | N.source_tokens(leg.resolution_source),
                        {"indicator": "fomc", "period": period, "kind": kind})
    if "CPI" in st:
        strike_type = (m.get("strike_type") or "").lower()
        floor_, cap = m.get("floor_strike"), m.get("cap_strike")
        if strike_type in ("greater", "greater_or_equal") and floor_ is not None:
            cmp_, thr = "above", float(floor_)
        elif strike_type in ("less", "less_or_equal") and cap is not None:
            cmp_, thr = "below", float(cap)
        else:
            return None
        mon = N.find_month(text)
        year = when.year if not (mon and mon > when.month) else when.year - 1
        if not mon:
            ym = N.ticker_month(m.get("event_ticker"))
            if not ym:
                return None
            year, mon = ym
        indicator = "cpi_yoy" if "YOY" in st or "year" in text.lower() else "cpi_mom"
        period = f"{year:04d}-{mon:02d}"
        key = f"macro|{indicator}|{period}|{cmp_}|{thr:g}"
        leg = _k_leg(m, series, title, yes_label="Yes")
        return CanonLeg("macro", key, key, when, leg, {"bls"} | N.source_tokens(leg.resolution_source),
                        {"indicator": indicator, "period": period, "cmp": cmp_, "threshold": thr})
    return None


def _game_end_ts(day: date) -> float:
    """Latest plausible end of a game played on `day` (Eastern): 06:00 ET the next morning.
    Venues often keep a market administratively open for days; capital is not locked that long."""
    return N.et_datetime(day + timedelta(days=1), 6).timestamp()


def kalshi_sports(event: dict, series: dict, league: str, teams: N.TeamBook) -> list[CanonLeg]:
    """One CanonLeg per team market of a game event. Team identity comes from text, not tickers."""
    ev_text = " ".join(str(event.get(k) or "") for k in ("title", "sub_title"))
    ev_teams = teams.find(ev_text, league)
    out: list[CanonLeg] = []
    game_day = N.ticker_date(event.get("event_ticker"))
    for m in event.get("markets") or []:
        m_text = " ".join(str(m.get(k) or "") for k in ("title", "subtitle", "yes_sub_title"))
        cands = ev_teams if len(ev_teams) == 2 else teams.find(f"{ev_text} {m_text}", league)
        if len(cands) != 2:
            continue
        side = teams.resolve(str(m.get("yes_sub_title") or ""), league, cands) \
            or teams.resolve(m_text, league, cands)
        if not side:
            continue
        when = _k_close(m)
        day = game_day or (N.et_date(when - timedelta(hours=5)) if when else None)
        if not day or not when:
            continue
        a, b = sorted(cands)
        key = f"sports|{league}|{day.isoformat()}|{side}"
        block = f"sports|{league}|{a}|{b}|{side}"
        title = f"{event.get('title', '')}: {m.get('yes_sub_title') or side}"
        leg = _k_leg(m, series, title, yes_label=str(m.get("yes_sub_title") or side))
        leg.close_ts = min(leg.close_ts or float("inf"), _game_end_ts(day))
        out.append(CanonLeg("sports", key, block, datetime(day.year, day.month, day.day, tzinfo=N.ET), leg,
                            {"official"}, {"league": league, "teams": [a, b], "side": side, "day": day.isoformat()}))
    return out


# ------------------------------------------------------------------------------------------------
# Polymarket
# ------------------------------------------------------------------------------------------------

_P_CRYPTO = re.compile(
    r"\b(bitcoin|btc|ethereum|ether|eth|solana|sol|xrp|ripple)\b.*?"
    r"\b(above|below|higher than|lower than|over|under|greater than|less than|at or above|at or below)\b"
    r"\s*\$?\s*(\d[\d,]*(?:\.\d+)?\s*[kKmM]?)", re.I)


def _p_tags(event: dict) -> list[str]:
    return [str(t.get("slug") or t.get("label") or "").lower() for t in (event.get("tags") or []) if isinstance(t, dict)]


def _p_leg(event: dict, m: dict, fees: FeeModel, category: str, yes_token: str, no_token: str,
           title: str, yes_label: str, source: str) -> Leg:
    end = N.parse_iso(m.get("endDate")) or N.parse_iso(event.get("endDate"))
    return Leg(
        venue=POLYMARKET, market_id=str(m.get("conditionId") or m.get("condition_id") or ""),
        title=title, yes_label=yes_label, fee_category=category, fee_multiplier=1.0,
        close_ts=N.to_ts(end) or 0.0, resolution_source=source, resolution_ts=N.to_ts(end),
        yes_token=yes_token, no_token=no_token,
        volume=float(m.get("volume24hr") or 0.0),
        extra={"event_slug": event.get("slug"), "slug": m.get("slug"), "neg_risk": bool(m.get("negRisk")),
               "description": (m.get("description") or "")[:400]},
    )


def _yes_no_tokens(m: dict) -> tuple[str, str] | None:
    outcomes = [str(o) for o in jlist(m.get("outcomes"))]
    tokens = [str(t) for t in jlist(m.get("clobTokenIds"))]
    if len(outcomes) != 2 or len(tokens) != 2:
        return None
    if outcomes[0].lower() == "yes":
        return tokens[0], tokens[1]
    if outcomes[1].lower() == "yes":
        return tokens[1], tokens[0]
    return None


def poly_crypto(event: dict, m: dict, fees: FeeModel) -> CanonLeg | None:
    q = str(m.get("question") or "")
    ql = q.lower()
    if "up or down" in ql or "reach" in ql or "hit" in ql or "dip" in ql or "all-time" in ql:
        return None
    mm = _P_CRYPTO.search(q)
    if not mm:
        return None
    asset = N.ASSETS.get(mm.group(1).lower())
    thr = N.parse_number(mm.group(3))
    if not asset or thr is None:
        return None
    kind = "above" if mm.group(2).lower() in ("above", "higher than", "over", "greater than", "at or above") else "below"
    tokens = _yes_no_tokens(m)
    if not tokens:
        return None
    end = N.parse_iso(m.get("endDate"))
    md = N.find_month_day(q)
    desc = str(m.get("description") or "")
    clock = N.find_clock_et(desc) or N.find_clock_et(q)
    if md:
        year = md[2] or (end.year if end else datetime.now(N.UTC).year)
        try:
            day = date(year, md[0], md[1])
        except ValueError:
            return None
        when = N.et_datetime(day, *clock) if clock else (end or N.et_datetime(day, 12))
    elif end:
        when = N.et_datetime(N.et_date(end), *clock) if clock else end
    else:
        return None
    instant = when.astimezone(N.UTC).strftime("%Y-%m-%dT%H:%MZ")
    key = f"crypto|{asset}|{kind}|{thr:g}|{instant}"
    block = f"crypto|{asset}|{kind}|{thr:g}|{N.et_date(when).isoformat()}"
    src_tokens = N.source_tokens(desc)
    leg = _p_leg(event, m, fees, "crypto", tokens[0], tokens[1], q, "Yes",
                 source=",".join(sorted(src_tokens)) or str(m.get("resolutionSource") or ""))
    return CanonLeg("crypto", key, block, when, leg, src_tokens,
                    {"asset": asset, "kind": kind, "threshold": f"{thr:g}", "instant": instant})


_P_CPI = re.compile(r"\bcpi\b.*?\b(above|below|higher than|lower than|over|under|greater than|less than)\b\s*"
                    r"(\d+(?:\.\d+)?)\s*%", re.I)


def poly_macro(event: dict, m: dict, fees: FeeModel) -> CanonLeg | None:
    q = str(m.get("question") or "")
    ev_title = str(event.get("title") or "")
    text = f"{q} {ev_title}"
    tl = text.lower()
    tokens = _yes_no_tokens(m)
    if not tokens:
        return None
    end = N.parse_iso(m.get("endDate"))
    if re.search(r"\bfed\b|federal reserve|fomc", tl) and re.search(r"bps|basis point|rate", tl):
        kind = fomc_kind(q) or fomc_kind(str(m.get("groupItemTitle") or ""))
        if not kind:
            return None
        mm = re.search(r"(january|february|march|april|may|june|july|august|september|october|november|december)"
                       r"\s*(\d{4})?", tl)
        if not mm:
            return None
        mon = N.MONTHS[mm.group(1)]
        year = int(mm.group(2)) if mm.group(2) else (end.year if end else datetime.now(N.UTC).year)
        period = f"{year:04d}-{mon:02d}"
        key = f"macro|fomc|{period}|{kind}"
        leg = _p_leg(event, m, fees, "economics", tokens[0], tokens[1], q, "Yes", source="fed")
        return CanonLeg("macro", key, key, end, leg, {"fed"}, {"indicator": "fomc", "period": period, "kind": kind})
    cm = _P_CPI.search(text)
    if cm:
        cmp_ = "above" if cm.group(1).lower() in ("above", "higher than", "over", "greater than") else "below"
        thr = float(cm.group(2))
        mon = N.find_month(text)
        if not mon:
            return None
        ym = re.search(r"\b(20\d{2})\b", text)
        year = int(ym.group(1)) if ym else (end.year if end else datetime.now(N.UTC).year)
        indicator = "cpi_yoy" if re.search(r"year[- ]over[- ]year|yoy|annual", tl) else "cpi_mom"
        period = f"{year:04d}-{mon:02d}"
        key = f"macro|{indicator}|{period}|{cmp_}|{thr:g}"
        leg = _p_leg(event, m, fees, "economics", tokens[0], tokens[1], q, "Yes", source="bls")
        return CanonLeg("macro", key, key, end, leg, {"bls"},
                        {"indicator": indicator, "period": period, "cmp": cmp_, "threshold": thr})
    return None


def poly_league(event: dict, leagues: list[str]) -> str | None:
    tags = _p_tags(event)
    title = str(event.get("title") or "").upper()
    for lg in leagues:
        if lg.lower() in tags or title.startswith(f"{lg}:") or f" {lg} " in f" {title} ":
            return lg
    return None


def poly_sports(event: dict, m: dict, fees: FeeModel, league: str, teams: N.TeamBook) -> list[CanonLeg]:
    """Two-outcome moneyline -> two legs (each team's win as canonical YES)."""
    smt = str(m.get("sportsMarketType") or "").lower()
    q = str(m.get("question") or "")
    if smt and smt != "moneyline":
        return []
    if not smt and re.search(r"spread|o/u|over/under|total|handicap|\+\d|-\d+\.\d", q, re.I):
        return []
    outcomes = [str(o) for o in jlist(m.get("outcomes"))]
    tokens = [str(t) for t in jlist(m.get("clobTokenIds"))]
    if len(outcomes) != 2 or len(tokens) != 2 or outcomes[0].lower() in ("yes", "no"):
        return []
    codes = [teams.find(o, league) for o in outcomes]
    if any(len(c) != 1 for c in codes) or codes[0][0] == codes[1][0]:
        return []
    start = N.parse_iso(m.get("gameStartTime")) or N.parse_iso(event.get("startDate")) or N.parse_iso(m.get("endDate"))
    if not start:
        return []
    day = N.et_date(start)
    a, b = sorted([codes[0][0], codes[1][0]])
    out: list[CanonLeg] = []
    for i in (0, 1):
        side = codes[i][0]
        key = f"sports|{league}|{day.isoformat()}|{side}"
        block = f"sports|{league}|{a}|{b}|{side}"
        leg = _p_leg(event, m, fees, "sports", tokens[i], tokens[1 - i], f"{q}: {outcomes[i]}", outcomes[i],
                     source="official")
        leg.extra["game_start"] = start.isoformat()
        leg.close_ts = min(leg.close_ts or float("inf"), _game_end_ts(day))
        out.append(CanonLeg("sports", key, block, datetime(day.year, day.month, day.day, tzinfo=N.ET), leg,
                            {"official"}, {"league": league, "teams": [a, b], "side": side, "day": day.isoformat()}))
    return out


def poly_family(event: dict, m: dict, cfg: UniverseCfg, fees: FeeModel, teams: N.TeamBook) -> list[CanonLeg]:
    """Route a Gamma market to its parser. Returns [] when no family claims it."""
    league = poly_league(event, cfg.leagues)
    if league:
        return poly_sports(event, m, fees, league, teams)
    tags = set(_p_tags(event))
    ql = str(m.get("question") or "").lower()
    if tags & {"crypto", "bitcoin", "ethereum", "solana", "xrp"} or re.search(r"\b(bitcoin|btc|ethereum|eth|solana|xrp)\b", ql):
        c = poly_crypto(event, m, fees)
        return [c] if c else []
    if tags & {"economy", "economics", "fed", "inflation", "cpi", "fed-rates", "interest-rates"} \
            or re.search(r"\bfed\b|fomc|\bcpi\b", ql):
        c = poly_macro(event, m, fees)
        return [c] if c else []
    return []


def kalshi_family(family: str, m: dict, series: dict) -> CanonLeg | None:
    if family == "crypto":
        return kalshi_crypto(m, series)
    if family == "macro":
        return kalshi_macro(m, series)
    return None


def kalshi_price_hint(m: dict) -> float | None:
    return dollars(m, "yes_bid")
