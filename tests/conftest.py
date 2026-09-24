from __future__ import annotations

from pathlib import Path

import pytest

from pmxarb.config import Config, load_config
from pmxarb.fees import FeeModel
from pmxarb.matching.normalize import TeamBook
from pmxarb.models import KALSHI, POLYMARKET, Leg

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cfg(tmp_path) -> Config:
    c = load_config(ROOT / "config.yaml")
    c.data_dir = str(tmp_path / "data")
    c.universe.teams_file = str(ROOT / "config" / "teams.yaml")
    c.universe.overrides_file = str(ROOT / "config" / "pair_overrides.yaml")
    return c


@pytest.fixture
def fees(cfg) -> FeeModel:
    return FeeModel(cfg.venues.kalshi, cfg.venues.polymarket)


@pytest.fixture
def teams(cfg) -> TeamBook:
    return TeamBook.load(cfg.universe.teams_file)


def kalshi_leg(ticker="KXTEST-1", series="KXNFLGAME", mult=1.0, yes_is_no=False, close_ts=2e9) -> Leg:
    return Leg(KALSHI, ticker, "test", "Yes", series, mult, close_ts, "official", None, yes_is_venue_no=yes_is_no)


def poly_leg(cond="0xabc", cat="sports", yes="tok_yes", no="tok_no", close_ts=2e9) -> Leg:
    return Leg(POLYMARKET, cond, "test", "Yes", cat, 1.0, close_ts, "official", None, yes_token=yes, no_token=no)
