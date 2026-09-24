from __future__ import annotations

import json

from pmxarb.matching.families import (fomc_kind, kalshi_crypto, kalshi_macro, kalshi_sports, poly_crypto, poly_family,
                                      poly_macro, poly_sports)
from pmxarb.matching.matcher import Overrides, build_pairs
from pmxarb.matching.normalize import find_clock_et, find_month_day, parse_number, ticker_date

SERIES_BTC = {"ticker": "KXBTCD", "fee_multiplier": 1.0, "fee_type": "quadratic",
              "settlement_sources": [{"name": "CF Benchmarks", "url": "https://cfbenchmarks.com"}]}
SERIES_NFL = {"ticker": "KXNFLGAME", "fee_multiplier": 1.0, "fee_type": "quadratic", "settlement_sources": [{"name": "NFL"}]}
SERIES_FED = {"ticker": "KXFEDDECISION", "fee_multiplier": 1.0, "settlement_sources": [{"name": "Federal Reserve"}]}


def k_market(**kw):
    base = {"ticker": "KXBTCD-26SEP18-T115000", "series_ticker": "KXBTCD", "event_ticker": "KXBTCD-26SEP18",
            "title": "Bitcoin price on Sep 18, 2026 at 5pm EDT?", "yes_sub_title": "$115,000 or above",
            "strike_type": "greater", "floor_strike": 115000, "close_time": "2026-09-18T21:00:00Z",
            "expected_expiration_time": "2026-09-18T21:00:00Z", "volume_fp": "1000.00", "rules_primary": "CF Benchmarks BRTI"}
    base.update(kw)
    return base


def p_event(tags, markets, title="Event"):
    return {"title": title, "slug": "ev", "tags": [{"slug": t} for t in tags], "markets": markets, "endDate": "2026-09-19T00:00:00Z"}


def p_market(question, description="", outcomes=("Yes", "No"), tokens=("t_yes", "t_no"), **kw):
    m = {"question": question, "description": description, "conditionId": "0x" + str(abs(hash(question)))[:8],
         "outcomes": json.dumps(list(outcomes)), "clobTokenIds": json.dumps(list(tokens)),
         "endDate": "2026-09-18T16:00:00Z", "volume24hr": 50000, "enableOrderBook": True}
    m.update(kw)
    return m


def test_normalize_helpers():
    assert parse_number("$115,000") == 115000
    assert parse_number("115k") == 115000
    assert parse_number("0.3%") == 0.3
    assert find_month_day("Will Bitcoin be above $115,000 on September 18?") == (9, 18, None)
    assert find_clock_et("the Binance 1 minute candle at 12:00 PM ET") == (12, 0)
    assert find_clock_et("at noon ET") == (12, 0)
    assert find_clock_et("5pm EDT") == (17, 0)
    assert ticker_date("KXNFLGAME-26SEP18DALNYG").isoformat() == "2026-09-18"
    assert fomc_kind("Fed decreases interest rates by 25 bps") == "cut_25"
    assert fomc_kind("Cut 50+ bps") == "cut_50plus"
    assert fomc_kind("No change") == "hold"
    assert fomc_kind("Hike 25 bps") == "hike_25"


def test_kalshi_crypto_parser():
    leg = kalshi_crypto(k_market(), SERIES_BTC)
    assert leg is not None
    assert leg.key == "crypto|BTC|above|115000|2026-09-18T21:00Z"
    assert leg.block == "crypto|BTC|above|115000|2026-09-18"
    assert "cfbenchmarks" in leg.sources
    assert kalshi_crypto(k_market(strike_type="between", floor_strike=1, cap_strike=None), SERIES_BTC) is None
    below = kalshi_crypto(k_market(strike_type="less", cap_strike=110000, floor_strike=None), SERIES_BTC)
    assert below.key.startswith("crypto|BTC|below|110000")


def test_poly_crypto_parser(fees):
    ev = p_event(["crypto", "bitcoin"], [])
    m = p_market("Will the price of Bitcoin be above $115,000 on September 18?",
                 "This market will resolve to Yes if the Binance 1 minute candle for BTC/USDT 18 Sep '26 12:00 in the ET timezone "
                 "(noon) has a final Close price higher than 115,000.")
    leg = poly_crypto(ev, m, fees)
    assert leg is not None
    assert leg.key == "crypto|BTC|above|115000|2026-09-18T16:00Z"       # noon ET = 16:00Z in September
    assert leg.block == "crypto|BTC|above|115000|2026-09-18"
    assert leg.sources == {"binance"}
    assert leg.leg.fee_category == "crypto"
    assert poly_crypto(ev, p_market("Bitcoin Up or Down - September 18, 10AM ET"), fees) is None
    assert poly_crypto(ev, p_market("Will Bitcoin reach $150k by December 31?"), fees) is None


def test_crypto_pair_is_basis_when_sources_and_instants_differ(cfg, fees):
    k = kalshi_crypto(k_market(), SERIES_BTC)
    ev = p_event(["crypto"], [])
    p = poly_crypto(ev, p_market("Will Bitcoin be above $115,000 on September 18?",
                                 "Binance BTCUSDT 1 minute candle at 12:00 PM ET"), fees)
    pairs, stats = build_pairs([k], [p], cfg, Overrides({}))
    assert len(pairs) == 1
    pr = pairs[0]
    assert pr.klass == "basis"
    assert any("instants differ" in n for n in pr.notes) and any("sources differ" in n for n in pr.notes)
    assert stats["crypto"]["basis"] == 1


def test_crypto_pair_exact_when_everything_agrees(cfg, fees):
    k = kalshi_crypto(k_market(close_time="2026-09-18T16:00:00Z", expected_expiration_time="2026-09-18T16:00:00Z",
                               rules_primary="Binance BTCUSDT"), dict(SERIES_BTC, settlement_sources=[{"name": "Binance"}]))
    p = poly_crypto(p_event(["crypto"], []), p_market("Will Bitcoin be above $115,000 on September 18?",
                                                        "Binance BTCUSDT 1 minute candle at 12:00 PM ET"), fees)
    pairs, _ = build_pairs([k], [p], cfg, Overrides({}))
    assert pairs and pairs[0].klass == "exact"


def test_fomc_parsers_match_exactly(cfg, fees):
    km = {"ticker": "KXFEDDECISION-26SEP-C25", "series_ticker": "KXFEDDECISION", "event_ticker": "KXFEDDECISION-26SEP",
          "title": "Fed decision in September?", "yes_sub_title": "Cut 25 bps", "close_time": "2026-09-16T18:00:00Z", "volume_fp": "5000.00"}
    k = kalshi_macro(km, SERIES_FED)
    assert k and k.key == "macro|fomc|2026-09|cut_25"
    ev = p_event(["economy", "fed"], [], title="Fed decision in September?")
    p = poly_macro(ev, p_market("Fed decreases interest rates by 25 bps after September 2026 meeting?"), fees)
    assert p and p.key == "macro|fomc|2026-09|cut_25"
    pairs, _ = build_pairs([k], [p], cfg, Overrides({}))
    assert pairs and pairs[0].klass == "exact"
    # 50+ on one side and exactly 50 on the other must not pair
    p2 = poly_macro(ev, p_market("Fed decreases interest rates by 50+ bps after September 2026 meeting?"), fees)
    k2 = kalshi_macro(dict(km, yes_sub_title="Cut 50 bps", ticker="KXFEDDECISION-26SEP-C50"), SERIES_FED)
    assert not build_pairs([k2], [p2], cfg, Overrides({}))[0]


def test_sports_both_sides_pair_with_polarity(cfg, fees, teams):
    event = {"event_ticker": "KXNFLGAME-26SEP18DALNYG", "title": "Dallas Cowboys at New York Giants",
             "markets": [{"ticker": "KXNFLGAME-26SEP18DALNYG-DAL", "yes_sub_title": "Dallas", "title": "Cowboys at Giants Winner?",
                          "close_time": "2026-09-19T03:30:00Z", "volume_fp": "900.00"},
                         {"ticker": "KXNFLGAME-26SEP18DALNYG-NYG", "yes_sub_title": "New York", "title": "Cowboys at Giants Winner?",
                          "close_time": "2026-09-19T03:30:00Z", "volume_fp": "900.00"}]}
    k_legs = kalshi_sports(event, SERIES_NFL, "NFL", teams)
    assert [l.meta["side"] for l in k_legs] == ["DAL", "NYG"]
    ev = p_event(["sports", "nfl"], [], title="NFL: Cowboys vs. Giants")
    m = p_market("Cowboys vs. Giants", outcomes=("Cowboys", "Giants"), tokens=("t_dal", "t_nyg"),
                 gameStartTime="2026-09-19 00:20:00+00", sportsMarketType="moneyline")
    p_legs = poly_sports(ev, m, fees, "NFL", teams)
    assert len(p_legs) == 2
    dal = next(l for l in p_legs if l.meta["side"] == "DAL")
    assert dal.leg.yes_token == "t_dal" and dal.leg.no_token == "t_nyg"
    nyg = next(l for l in p_legs if l.meta["side"] == "NYG")
    assert nyg.leg.yes_token == "t_nyg" and nyg.leg.no_token == "t_dal"
    pairs, stats = build_pairs(k_legs, p_legs, cfg, Overrides({}))
    assert len(pairs) == 2 and all(p.klass == "exact" for p in pairs)
    assert stats["sports"]["exact"] == 2
    # spreads and totals are ignored
    assert poly_sports(ev, p_market("Cowboys vs. Giants: Spread", outcomes=("Cowboys -3.5", "Giants +3.5"), sportsMarketType="spreads"),
                       fees, "NFL", teams) == []
    # router picks sports from tags
    assert len(poly_family(ev, m, cfg.universe, fees, teams)) == 2


def test_sports_loose_date_join_and_overrides(cfg, fees, teams):
    event = {"event_ticker": "KXNFLGAME-26SEP19DALNYG", "title": "Dallas Cowboys at New York Giants",
             "markets": [{"ticker": "X-DAL", "yes_sub_title": "Dallas", "title": "", "close_time": "2026-09-19T03:30:00Z", "volume_fp": "1.00"}]}
    k_legs = kalshi_sports(event, SERIES_NFL, "NFL", teams)
    ev = p_event(["nfl"], [])
    m = p_market("Cowboys vs. Giants", outcomes=("Cowboys", "Giants"), tokens=("t_dal", "t_nyg"), gameStartTime="2026-09-19 00:20:00+00")
    p_legs = [l for l in poly_sports(ev, m, fees, "NFL", teams) if l.meta["side"] == "DAL"]
    pairs, _ = build_pairs(k_legs, p_legs, cfg, Overrides({}))
    assert len(pairs) == 1 and pairs[0].klass == "exact" and any("game day differs" in n for n in pairs[0].notes)
    ov = Overrides({"exclude": [{"key_regex": r"^sports\|NFL"}]})
    assert build_pairs(k_legs, p_legs, cfg, ov)[0] == []
    ov2 = Overrides({"reclass": [{"pair_id": pairs[0].pair_id, "klass": "basis", "note": "test"}]})
    assert build_pairs(k_legs, p_legs, cfg, ov2)[0][0].klass == "basis"


def test_team_disambiguation(teams):
    assert teams.find("New York Giants at Dallas Cowboys", "NFL") == ["NYG", "DAL"]
    assert teams.resolve("New York", "NFL", ["NYG", "DAL"]) == "NYG"
    assert teams.resolve("New York", "NFL", ["NYG", "NYJ"]) is None          # ambiguous stays unresolved
    assert teams.find("Chicago White Sox vs Chicago Cubs", "MLB") == ["CWS", "CHC"]
    assert teams.find("Was the game in Washington?", "NFL") == ["WAS"]         # 'was' the verb is not a team


def test_teams_yaml_has_only_string_codes(teams):
    # regression: an unquoted NO key is parsed by YAML as False and crashed the sports parser
    assert "NO" in teams.table["NFL"]
    assert all(isinstance(c, str) for lg in teams.table.values() for c in lg)
    assert teams.find("New Orleans Saints at Atlanta Falcons", "NFL") == ["NO", "ATL"]
    with __import__("pytest").raises(ValueError):
        __import__("pmxarb.matching.normalize", fromlist=["TeamBook"]).TeamBook({"NFL": {False: ["Saints"]}})


def test_crypto_loose_join_respects_instant_tolerance(cfg, fees):
    k = kalshi_crypto(k_market(close_time="2026-09-18T23:00:00Z", expected_expiration_time="2026-09-18T23:00:00Z"), SERIES_BTC)
    p = poly_crypto(p_event(["crypto"], []), p_market("Will Bitcoin be above $115,000 on September 18?",
                                                        "Binance BTCUSDT 1 minute candle at 12:00 PM ET"), fees)
    assert k.block == p.block                    # same ET day, same threshold
    pairs, stats = build_pairs([k], [p], cfg, Overrides({}))
    assert pairs == [] and stats["crypto"]["kalshi_unmatched"] == 1   # 7 hours apart: not even a basis pair


def test_fomc_kind_kalshi_wording_from_production():
    # Real Kalshi titles seen on 2026-09-18: '>25bps' is strictly more than 25, i.e. 50 or more
    assert fomc_kind("Will the Federal Reserve Hike rates by >25bps at their October 2026 meeting?") == "hike_50plus"
    assert fomc_kind("Will the Federal Reserve Cut rates by >25bps at their October 2026 meeting?") == "cut_50plus"
    assert fomc_kind("Will the Federal Reserve Hike rates by 0bps at their October 2026 meeting?") == "hold"
    assert fomc_kind("Will the Federal Reserve Cut rates by 25bps at their October 2026 meeting?") == "cut_25"
    # Real Polymarket wording
    assert fomc_kind("Will the Fed increase interest rates by 25 bps after the October 2026 meeting?") == "hike_25"
    assert fomc_kind("Will the Fed decrease interest rates by 50+ bps after the October 2026 meeting?") == "cut_50plus"
    assert fomc_kind("Will there be no change in Fed interest rates after the October 2026 meeting?") == "hold"
    assert fomc_kind("Fed decreases interest rates by at least 50 bps") == "cut_50plus"


def test_fomc_strict_more_than_does_not_pair_with_exact(cfg, fees):
    km = {"ticker": "KXFEDDECISION-26OCT-H26", "series_ticker": "KXFEDDECISION", "event_ticker": "KXFEDDECISION-26OCT",
          "title": "Will the Federal Reserve Hike rates by >25bps at their October 2026 meeting?", "close_time": "2026-10-28T18:00:00Z",
          "volume_fp": "100.00"}
    k = kalshi_macro(km, SERIES_FED)
    ev = p_event(["fed"], [], title="Fed decision in October?")
    p = poly_macro(ev, p_market("Will the Fed increase interest rates by 25 bps after the October 2026 meeting?",
                                endDate="2026-10-28T20:00:00Z"), fees)
    assert k.key == "macro|fomc|2026-10|hike_50plus" and p.key == "macro|fomc|2026-10|hike_25"
    assert build_pairs([k], [p], cfg, Overrides({}))[0] == []


def test_sports_close_is_capped_to_game_day(cfg, fees, teams):
    event = {"event_ticker": "KXMLBGAME-26SEP18KCPIT", "title": "Kansas City vs Pittsburgh",
             "markets": [{"ticker": "X-KC", "yes_sub_title": "Kansas City", "title": "", "close_time": "2026-09-22T23:00:00Z", "volume_fp": "1.00"}]}
    k = kalshi_sports(event, SERIES_NFL, "MLB", teams)[0]
    from datetime import datetime, timezone
    assert datetime.fromtimestamp(k.leg.close_ts, tz=timezone.utc).date().isoformat() == "2026-09-19"     # 06:00 ET next day
    m = p_market("Royals vs. Pirates", outcomes=("Kansas City Royals", "Pittsburgh Pirates"), tokens=("t_kc", "t_pit"),
                 gameStartTime="2026-09-18 22:40:00+00", endDate="2026-09-22T23:00:00Z", sportsMarketType="moneyline")
    p = poly_sports(p_event(["mlb"], []), m, fees, "MLB", teams)[0]
    assert abs(p.leg.close_ts - k.leg.close_ts) < 1
