from __future__ import annotations

import json

from pmxarb.cli import _other_game, cmd_final


def _pair(pid, key, kalshi_id, start, family="sports"):
    return {"pair_id": pid, "family": family, "key": key, "klass": "exact",
            "legs": {"kalshi": {"market_id": kalshi_id}, "polymarket": {"extra": {"game_start": start}}}}


# archived definitions of 22-23 September 2026
SAME = _pair("sports-0e05689d6d", "sports|MLB|2026-09-23|HOU", "KXMLBGAME-26SEP232210HOUSEA-HOU", "2026-09-24T02:10:00+00:00")
OTHER_SAME_DAY = _pair("sports-92c069c3e2", "sports|MLB|2026-09-23|TOR", "KXMLBGAME-26SEP231835TORBAL-TOR",
                       "2026-09-23T17:35:00+00:00")
OTHER_DAY = _pair("sports-3e8d969198", "sports|MLB|2026-09-22|TOR", "KXMLBGAME-26SEP221835TORBAL-TOR",
                  "2026-09-23T22:35:00+00:00")
NFL = _pair("sports-8a596dcf5b", "sports|NFL|2026-09-20|BAL", "KXNFLGAME-26SEP20NOBAL-BAL", "2026-09-20T17:00:00+00:00")


def test_other_game_rule_on_archived_pairs():
    assert not _other_game(SAME)
    assert _other_game(OTHER_SAME_DAY)          # same day, five hours apart: the other game of the day
    assert _other_game(OTHER_DAY)               # next day's game
    assert not _other_game(NFL)                 # no start in the ticker: same Eastern day is enough
    assert _other_game(dict(NFL, key="sports|NFL|2026-09-21|BAL"))
    assert not _other_game(dict(SAME, family="macro"))


def test_final_sets_the_other_games_apart(cfg, capsys):
    (cfg.data / "universe").mkdir(parents=True, exist_ok=True)
    (cfg.data / "blotter").mkdir(parents=True, exist_ok=True)
    (cfg.data / "universe" / "pairs_20260923-050000.json").write_text(json.dumps({"pairs": [SAME, OTHER_DAY]}))
    rows = [{"ts": 1_790_000_000, "pair_id": SAME["pair_id"], "klass": "exact", "divergent": False, "pnl": 3.0},
            {"ts": 1_790_000_000, "pair_id": OTHER_DAY["pair_id"], "klass": "exact", "divergent": True, "pnl": -40.0},
            {"ts": 1_780_000_000, "pair_id": SAME["pair_id"], "klass": "exact", "divergent": False, "pnl": 1.0}]
    (cfg.data / "blotter" / "resolutions.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    assert cmd_final(cfg, "2026-09-20") == 0
    out = capsys.readouterr().out
    assert "1 archived sports pairs joined two different games" in out and OTHER_DAY["pair_id"] in out
    assert "| False | 1 | 0 | 3.0 |" in out and "| True | 1 | 1 | -40.0 |" in out


def test_final_runs_when_no_pair_is_flagged(cfg, capsys):
    (cfg.data / "universe").mkdir(parents=True, exist_ok=True)
    (cfg.data / "universe" / "pairs_20260923-050000.json").write_text(json.dumps({"pairs": [SAME]}))
    assert cmd_final(cfg, None) == 0
    assert "0 archived sports pairs joined two different games" in capsys.readouterr().out
