"""Typed configuration loaded from config.yaml. Validation happens once, at startup."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator


class KalshiCfg(BaseModel):
    base_url: str
    max_rps: float = 8
    orderbook_batch: int = 100
    candle_batch_periods: int = 4000
    taker_rate: float = 0.07
    maker_rate: float = 0.0175
    fee_rounding: float = 0.0001
    default_fee_multiplier: float = 1.0
    series: dict[str, list[str]] = Field(default_factory=dict)


class PolymarketCfg(BaseModel):
    gamma_url: str
    clob_url: str
    max_rps: float = 8
    book_batch: int = 50
    events_page: int = 200
    history_points: int = 720
    taker_rates: dict[str, float]
    tag_categories: dict[str, str] = Field(default_factory=dict)


class VenuesCfg(BaseModel):
    kalshi: KalshiCfg
    polymarket: PolymarketCfg


class UniverseCfg(BaseModel):
    refresh_utc_hour: int = 5
    min_volume_24h_usd: float = 2000
    min_kalshi_volume: float = 200
    max_days_to_close: int = 45
    leagues: list[str] = Field(default_factory=lambda: ["NFL", "MLB", "NBA"])
    sports_date_tolerance_days: int = 1
    overrides_file: str = "config/pair_overrides.yaml"
    teams_file: str = "config/teams.yaml"


class RecorderCfg(BaseModel):
    poll_seconds: float = 3.0
    depth_levels: int = 5
    record_mode: str = "changes"
    rotate_minutes: int = 60
    gzip_after_minutes: int = 120


class ScannerCfg(BaseModel):
    min_edge: float = 0.002
    min_qty: float = 5
    min_level_size: float = 2


class PaperCfg(BaseModel):
    capital_per_venue_usd: float = 10000
    max_notional_per_pair_usd: float = 500
    # Classes the desk is allowed to put capital into. `basis` pairs are still detected, recorded
    # and settled, but trading them is a short-volatility carry on the settlement window, not the
    # arbitrage this study measures: it consumes the budget, carries leg risk, and answers a
    # different question. Their counterfactual result is computed in the report at zero risk.
    execute_classes: list[str] = Field(default_factory=lambda: ["exact"])
    # Per-class notional ceiling, applied on top of execute_classes.
    max_notional_per_class_usd: dict[str, float] = Field(default_factory=dict)
    # How the two legs are filled against the next snapshot.
    #   joint      the executor re-walks both ladders together and takes only contracts that are
    #              still jointly profitable: an upper bound, since a real taker cannot fill two
    #              venues atomically, but a fill can never cost more than the pair pays and a leg
    #              cannot be left naked.
    #   sequenced  the first leg is sent alone; the second leg's limit is whatever still leaves the
    #              hedge its required edge after what the first leg actually paid. The difference
    #              between the two models is the price of non-atomic execution.
    execution_model: str = "joint"
    # Fraction of the edge measured at detection that a fill must keep. 0 means any fill at or
    # below par is accepted; 1 means only fills as good as the detection. Replaces the former
    # absolute price tolerance, whose 1 cent per leg was five times the minimum edge and let
    # hedges fill above par.
    edge_retention: float = 0.5
    first_leg: str = "kalshi"        # sequenced only. Polymarket leads price discovery (Zhou, SSRN
    #                                  5331995), so the Kalshi quote is the one about to move.
    on_leg_failure: str = "unwind"
    failure_cooldown_polls: int = 20
    mtm_utc_hour: int = 6

    @field_validator("execution_model")
    @classmethod
    def _model(cls, v: str) -> str:
        if v not in ("joint", "sequenced"):
            raise ValueError("paper.execution_model must be joint or sequenced")
        return v

    @field_validator("edge_retention")
    @classmethod
    def _retention(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("paper.edge_retention must be between 0 and 1")
        return v

    @field_validator("first_leg")
    @classmethod
    def _first(cls, v: str) -> str:
        if v not in ("kalshi", "polymarket"):
            raise ValueError("paper.first_leg must be kalshi or polymarket")
        return v


class ResolutionCfg(BaseModel):
    sweep_minutes: int = 15
    # A pair half-settled for longer than this is booked as it stands, flagged, so that a venue
    # which never reports cannot hold a result in escrow for ever.
    escrow_max_days: float = 7.0


class ReportCfg(BaseModel):
    utc_hour: int = 6
    git_push: bool = False
    git_dir: str = "/data/reports_repo"


class HistoryCfg(BaseModel):
    enabled: bool = True
    weekday: int = 0
    utc_hour: int = 7
    days: int = 7
    top_pairs: int = 40
    poly_half_spread: float = 0.01


class TelegramCfg(BaseModel):
    enabled: bool = True


class Config(BaseModel):
    data_dir: str = "/data"
    venues: VenuesCfg
    universe: UniverseCfg = UniverseCfg()
    recorder: RecorderCfg = RecorderCfg()
    scanner: ScannerCfg = ScannerCfg()
    paper: PaperCfg = PaperCfg()
    resolution: ResolutionCfg = ResolutionCfg()
    report: ReportCfg = ReportCfg()
    history: HistoryCfg = HistoryCfg()
    telegram: TelegramCfg = TelegramCfg()

    @property
    def data(self) -> Path:
        return Path(self.data_dir)

    def path(self, *parts: str) -> Path:
        p = self.data.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p


def load_config(path: str | os.PathLike | None = None) -> Config:
    p = Path(path or os.environ.get("PMX_CONFIG", "config.yaml"))
    with open(p, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    cfg = Config.model_validate(raw)
    if os.environ.get("PMX_DATA_DIR"):
        cfg.data_dir = os.environ["PMX_DATA_DIR"]
    # relative helper files resolve against the config file, so the same config works in Docker and locally
    base = p.resolve().parent
    for attr in ("teams_file", "overrides_file"):
        v = Path(getattr(cfg.universe, attr))
        if not v.is_absolute():
            setattr(cfg.universe, attr, str(base / v))
    return cfg
